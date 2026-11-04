/** Load ONLY inside the approved Browser/CUA REPL. No separate browser attachment. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {installFarabiTiming} from '../data_collector/farabi_timing_probe.mjs';
import {createJournal,NetworkFilter,targetClass,writeReport,timingVersion,sanitizePageEvent} from './farabi_timing.mjs';

export async function startTiming(cdp,{workspace,isin='IRO3FSTZ0001',symbol='فکور',synthetic=false,
  capacity=2048,lifetimeMs=30*60000}={}){
  const base=path.resolve(workspace,synthetic?'tests/.timing-output':'data/private/farabi-timing');
  const session=path.join(base,`session-${new Date().toISOString().replace(/[:.]/g,'-')}-${crypto.randomBytes(3).toString('hex')}`);
  const source=`(${installFarabiTiming.toString()})(${JSON.stringify({isin,symbol,synthetic,capacity,lifetimeMs})})`;
  const implementationFiles={};
  for(const name of ['data_collector/farabi_timing_probe.mjs','scripts/farabi_timing.mjs','scripts/farabi_timing_cua.mjs'])
    implementationFiles[name]=crypto.createHash('sha256').update(await fs.readFile(path.join(workspace,name))).digest('hex');
  const journal=await createJournal(session,{synthetic,privateRoot:base,fixtureRoot:base});
  const gaps=[],documents=new Map(),stoppedDocuments=new Set(),droppedByDocument=new Map(),network=new NetworkFilter({synthetic}),ws=new Map();
  let observedRequestEvictions=0,wsEvictions=0,lastWsReceiveObservedAtMs=null,lastWsReceiveCdpTime=null;
  const manifest={version:timingVersion,mode:'in-app-browser-cua-passive',synthetic,isin,
    started_utc:new Date().toISOString(),health:'STARTING',missing_prefix:true,gaps,
    capabilities:{top_frame:true,cdp_network_metadata:true,page_resource_fallback:true,
      websocket_payloads:false,websocket_receive_age:'collector_observation_age_only',exchange_timestamp:false,restart_requires_chat_rearm:true,
      automatic_new_document_hook:false,poll_rearm_after_reload:true},
    source_sha256:crypto.createHash('sha256').update(source).digest('hex'),
    probe_function_sha256:crypto.createHash('sha256').update(installFarabiTiming.toString()).digest('hex'),
    implementation_file_sha256:implementationFiles};
  let cursor,scriptId,closed=false,lastPageHealth,lastNetworkPollUtc=null,lastPump=performance.now();
  const evaluate=async expression=>{
    const result=await cdp.send('Runtime.evaluate',{expression,returnByValue:true});
    if(result.exceptionDetails)throw new Error('Passive probe evaluation failed');
    return result.result?.value;
  };
  const gap=reason=>{gaps.push({reason,observed_utc:new Date().toISOString()});manifest.health='DEGRADED';};
  try{
    await cdp.send('Network.enable',{});
    const initial=await cdp.readEvents({methods:['Network.requestWillBeSent'],limit:1000,timeoutMs:0});cursor=initial.cursor;
    // The bounded prefix before our attach is declared missing; never represented as captured.
    // Some approved Browser providers do not expose Page.addScript*. This is
    // a capability limit, not permission to attach through another browser API.
    try{
      const registration=await cdp.send('Page.addScriptToEvaluateOnNewDocument',{source});scriptId=registration.identifier;
      manifest.capabilities.automatic_new_document_hook=Boolean(scriptId);
    }catch{manifest.capabilities.automatic_new_document_hook=false;}
    const install=await evaluate(source);manifest.install=install;
    if(!install?.installed||install.version!==timingVersion)throw new Error('Passive probe install or version check failed');
    await journal.append([{type:'collector.start',data:{version:timingVersion,mode:manifest.mode,missing_prefix:true,
      source_sha256:manifest.source_sha256,probe_function_sha256:manifest.probe_function_sha256,
      implementation_file_sha256:implementationFiles}}]);
    await fs.writeFile(path.join(session,'manifest.json'),JSON.stringify(manifest,null,2));
  }catch(error){
    if(scriptId)await cdp.send('Page.removeScriptToEvaluateOnNewDocument',{identifier:scriptId}).catch(()=>{});
    gap('attach_failed');await journal.append([{type:'collector.gap',data:{reason:'attach_failed'}}]).catch(()=>{});
    await journal.close();await writeReport(session,manifest);throw error;}
  const methods=['Network.requestWillBeSent','Network.responseReceived','Network.loadingFinished','Network.loadingFailed',
    'Network.webSocketCreated','Network.webSocketClosed','Network.webSocketFrameError',
    'Network.webSocketFrameReceived','Network.webSocketFrameSent'];
  async function drain({finalizing=false}={}){
    if(closed)throw new Error('Recorder already finalized');
    if(performance.now()-lastPump>8000)gap('collector_poll_gap');
    lastPump=performance.now();let records=[];
    try{
      let hasMore=true,rounds=0,framesReceived=0,framesSent=0;
      while(hasMore&&rounds++<20){
        const batch=await cdp.readEvents({afterSequence:cursor,methods,limit:1000,timeoutMs:0});cursor=batch.cursor;
        lastNetworkPollUtc=new Date().toISOString();
        if(batch.truncated){gap('cdp_event_buffer_truncated');records.push({type:'collector.gap',data:{reason:'cdp_event_buffer_truncated'}});}
        for(const event of batch.events){
          const clean=network.event(event);if(clean)records.push(clean);
          if(network.evictions>observedRequestEvictions){
            const count=network.evictions-observedRequestEvictions;observedRequestEvictions=network.evictions;
            gap('network_request_tracking_capacity_exceeded');
            records.push({type:'collector.gap',data:{reason:'network_request_tracking_capacity_exceeded',count}});
          }
          const p=event.params||{};
          if(event.method==='Network.webSocketCreated'){
            const target=targetClass(p.url,synthetic);
            if(target){
              if(!ws.has(p.requestId)&&ws.size>=100){ws.delete(ws.keys().next().value);wsEvictions++;gap('websocket_tracking_capacity_exceeded');
                records.push({type:'collector.gap',data:{reason:'websocket_tracking_capacity_exceeded',count:wsEvictions}});}
              ws.set(p.requestId,target);records.push({type:'websocket.open_observed',data:target});}
          }else if(ws.has(p.requestId)){
            if(event.method==='Network.webSocketFrameReceived'){framesReceived++;lastWsReceiveObservedAtMs=performance.now();
              lastWsReceiveCdpTime=typeof p.timestamp==='number'&&Number.isFinite(p.timestamp)?p.timestamp:null;}
            if(event.method==='Network.webSocketFrameSent')framesSent++;
            if(['Network.webSocketClosed','Network.webSocketFrameError'].includes(event.method)){
              records.push({type:'websocket.lifecycle',data:{...ws.get(p.requestId),event:event.method.endsWith('Closed')?'closed':'error'}});
              if(event.method.endsWith('Closed'))ws.delete(p.requestId);
            }
          }
        }
        hasMore=batch.hasMore;
      }
      if(hasMore)gap('network_backlog_limit');
      if(framesReceived||framesSent)records.push({type:'websocket.counts',data:{received:framesReceived,sent:framesSent,
        preexisting_websocket_coverage:'unknown',payload_captured:false}});
      records.push({type:'websocket.freshness',data:{known_open_connections:ws.size,market_data_freshness:'unknown',...(lastWsReceiveObservedAtMs===null?
        {age_since_collector_observed_ms:null,last_received_cdp_monotonic_seconds:null,status:'unknown_no_receive_frame_observed_after_attach',
          semantics:'CDP frame timestamp and collector monotonic clock are separate; no payload is stored'}:
        {age_since_collector_observed_ms:Math.max(0,performance.now()-lastWsReceiveObservedAtMs),
          last_received_cdp_monotonic_seconds:lastWsReceiveCdpTime,status:'age_since_collector_observed_frame',
          semantics:'Age since collector observed the last received frame; polling delay applies; not exact wire-arrival age'})} });
      let snapshot=await evaluate('window.__brsFarabiTimingV1?.read(0) || null');
      if(!snapshot){
        gap('page_probe_missing');records.push({type:'collector.gap',data:{reason:'page_probe_missing'}});
        if(finalizing){await journal.append(records);return {health:'DEGRADED',probe_present:false,probe_active:null};}
        const rearm=await evaluate(source);
        if(rearm?.installed&&rearm.version===timingVersion){
          records.push({type:'collector.rearm',data:{missing_interval:true}});
          snapshot=await evaluate('window.__brsFarabiTimingV1?.read(0) || null');
        }
        if(!snapshot){await journal.append(records);return {health:manifest.health,probe_present:false};}
      }
      const networkEvents=records.length;
      const value=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
      let pageEvents=0,durable=null,last=0,documentId=null,probeActive=null,caughtUp=false,pageRounds=0;
      while(snapshot&&pageRounds<32){
        pageRounds++;
        if(!/^page-\d+-\d+$/.test(snapshot.document_id||'')||!Number.isSafeInteger(snapshot.source_sequence)||
          snapshot.source_sequence<0||!Array.isArray(snapshot.events)||typeof snapshot.active!=='boolean'){
          gap('invalid_page_snapshot');records.push({type:'collector.gap',data:{reason:'invalid_page_snapshot'}});
          durable=await journal.append(records);records=[];break;
        }
        documentId=snapshot.document_id;probeActive=snapshot.active;
        const prior=documents.get(documentId)||0;
        if(!documents.has(documentId)&&documents.size)gap('navigation_boundary_missing_tail_possible');
        const anchor=snapshot.clock_sample||{};
        records.push({type:'clock.anchor',document_id:documentId,
          data:{time_origin_ms:value(snapshot.time_origin),page_before_ms:value(anchor.page_before_ms),
            page_after_ms:value(anchor.page_after_ms),device_wall_ms:value(anchor.device_wall_ms),
            utc_uncertainty_ms:null,basis:'paired_local_reads_not_independent_UTC'}});
        const pageRecords=snapshot.events.filter(e=>e.source_sequence>prior);
        if(pageRecords.some(e=>e.type==='clock.discontinuity'))gap('page_clock_discontinuity');
        const dropped=Number.isSafeInteger(snapshot.dropped)&&snapshot.dropped>=0?snapshot.dropped:null;
        if(dropped===null)gap('invalid_page_drop_count');
        else if(dropped>(droppedByDocument.get(documentId)||0)){
          droppedByDocument.set(documentId,dropped);gap('page_ring_overflow');
          records.push({type:'collector.gap',data:{reason:'page_ring_overflow',dropped}});
        }
        if(snapshot.source_sequence<prior||pageRecords.some((e,i)=>
          !Number.isSafeInteger(e.source_sequence)||e.source_sequence!==prior+i+1||e.source_sequence>snapshot.source_sequence))
          gap('page_source_sequence_gap');
        const noProgress=pageRecords.length===0&&snapshot.source_sequence>prior;
        if(noProgress)gap('page_snapshot_no_progress');
        const sanitized=pageRecords.map(sanitizePageEvent).filter(Boolean);
        if(sanitized.length!==pageRecords.length)gap('invalid_page_event');
        records.push(...sanitized);
        last=pageRecords.at(-1)?.source_sequence||prior;
        const complete=last>=snapshot.source_sequence&&!noProgress;
        if(!snapshot.active&&complete&&lastPageHealth!=='STOPPED'){
          lastPageHealth='STOPPED';records.push({type:'collector.probe_stopped',data:{}});
        }
        durable=await journal.append(records);records=[];
        documents.set(documentId,last);
        if(sanitized.some(e=>e.type==='session.stop'))stoppedDocuments.add(documentId);
        pageEvents+=pageRecords.length;
        const ack=await evaluate(`window.__brsFarabiTimingV1?.ack(${last},${gaps.length===0})`);
        if(ack!==last){gap('page_ack_mismatch');break;}
        if(noProgress)break;
        if(complete){caughtUp=true;break;}
        snapshot=await evaluate('window.__brsFarabiTimingV1?.read(0) || null');
        if(!snapshot){gap('page_probe_missing_during_drain');break;}
      }
      if(!caughtUp&&pageRounds>=32)gap('page_backlog_limit');
      if(finalizing&&(probeActive!==false||!documentId))gap('stop_probe_unconfirmed');
      if(finalizing&&documentId&&!stoppedDocuments.has(documentId))gap('session_stop_not_recorded');
      manifest.health=gaps.length?'DEGRADED':probeActive?'RECORDING':'STOPPED';
      await fs.writeFile(path.join(session,'health.json'),JSON.stringify({health:manifest.health,last_durable_ingest_sequence:durable,
        last_page_sequence:last,document_id:documentId,updated_utc:new Date().toISOString(),gaps:gaps.length}));
      return {health:manifest.health,probe_present:!!documentId,probe_active:probeActive,page_events:pageEvents,
        durable_sequence:durable,network_events:networkEvents,gaps:gaps.length};
    }catch{
      gap('capture_or_writer_failure');
      if(!journal.failed)await journal.append([{type:'collector.gap',data:{reason:'capture_or_writer_failure'}}]).catch(()=>{});
      return {health:'DEGRADED',gaps:gaps.length,writer_failed:journal.failed};
    }
  }
  const api={directory:session,manifest,
    async pump(seconds=15){
      if(!(seconds>=0&&seconds<=45))throw new Error('Pump duration must be 0..45 seconds');
      const end=performance.now()+seconds*1000;let result;
      do{result=await drain();if(performance.now()<end)await new Promise(r=>setTimeout(r,1000));}while(performance.now()<end);
      return {...result,directory:session};
    },
    async finish(){
      if(closed)throw new Error('Recorder already finalized');
      const stopped=await evaluate("window.__brsFarabiTimingV1 ? (window.__brsFarabiTimingV1.stop('operator_stop'),window.__brsFarabiTimingV1.active) : null")
        .catch(()=>null);
      if(stopped!==false)gap('stop_probe_unconfirmed');
      await drain({finalizing:true});
      // A second bounded network poll captures events that arrived while page
      // batches were flushed; events after the last poll are outside coverage.
      await drain({finalizing:true});
      if(scriptId)await cdp.send('Page.removeScriptToEvaluateOnNewDocument',{identifier:scriptId}).catch(()=>gap('reload_hook_remove_failed'));
      await journal.append([{type:'collector.stop',data:{gaps:gaps.length}}]).catch(()=>gap('final_write_failed'));
      await journal.close();closed=true;
      manifest.ended_utc=new Date().toISOString();manifest.health=gaps.length?'DEGRADED':'SAVED';
      manifest.network_observation={last_poll_collector_wall_utc:lastNetworkPollUtc,events_after_last_poll:'not_observed',
        tracked_requests_in_flight:network.requests.size};
      const metrics=await writeReport(session,manifest);
      await evaluate('window.__brsFarabiTimingV1?.markSaved()').catch(()=>{});
      return {directory:session,health:manifest.health,intents:metrics.intents.length,gaps:gaps.length};
    }};
  await api.pump(0);return api;
}

/** Keep start/pump/finish inside ONE fully awaited, approved CUA tool call.
 * No detached promises, background service, input or order sender.
 */
export async function captureTimingWindow(cdp,{durationSeconds=45,...options}={}){
  if(!Number.isFinite(durationSeconds)||durationSeconds<0.1||durationSeconds>45)
    throw new Error('Capture window must be 0.1..45 seconds');
  const recorder=await startTiming(cdp,{...options,lifetimeMs:Math.ceil((durationSeconds+15)*1000)});
  recorder.manifest.collector_driver={version:'window-1',mode:'single_awaited_tool_call',duration_seconds:durationSeconds};
  let pumpFailed=false;
  try{await recorder.pump(durationSeconds);}
  catch{
    pumpFailed=true;
    recorder.manifest.gaps.push({reason:'capture_window_failure',observed_utc:new Date().toISOString()});
    recorder.manifest.health='DEGRADED';
  }
  const receipt=await recorder.finish();
  return {...receipt,collector_driver:'window-1',requested_duration_seconds:durationSeconds,pump_failed:pumpFailed};
}
