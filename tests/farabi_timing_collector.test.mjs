/** Synthetic collector integration; fake CDP, real temporary fsync and reports. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {startTiming,captureTimingWindow} from '../scripts/farabi_timing_cua.mjs';
import {timingVersion} from '../scripts/farabi_timing.mjs';

class FakeCdp{
  constructor(){this.calls=[];this.sequence=0;this.document='page-1000000-1';this.events=[];this.networkEvents=[];this.networkSequence=0;this.active=true;this.truncated=false;this.fail=false;this.hook=true;this.probe=true;this.readLimit=512;this.hideEvents=false;this.badAck=false;this.installCalls=0;}
  add(type,data={}){this.events.push({document_id:this.document,clock_segment:1,source_sequence:++this.sequence,
    page_ms:100+this.sequence,device_wall_ms:1000000+this.sequence,synthetic:true,type,data});}
  addNetwork(method,params={}){this.networkEvents.push({sequence:++this.networkSequence,method,params});}
  async send(method,params={}){
    this.calls.push(method);
    if(method==='Page.addScriptToEvaluateOnNewDocument'){if(!this.hook)throw new Error('Synthetic unsupported capability');return {identifier:'synthetic-script'};}
    if(method!=='Runtime.evaluate')return {};
    const e=params.expression;
    if(e.includes('(function installFarabiTiming')){this.installCalls++;this.probe=true;this.add('session.start');return {result:{value:{installed:true,version:timingVersion,synthetic:true}}};}
    if(e.includes('.stop(')){if(!this.probe)return {result:{value:null}};if(this.active)this.add('session.stop',{reason:'operator_stop'});this.active=false;return {result:{value:false}};}
    if(e.includes('.markSaved('))return {result:{value:null}};
    if(e.includes('.read('))return {result:{value:this.probe?{version:timingVersion,document_id:this.document,time_origin:1000000,
      synthetic:true,active:this.active,source_sequence:this.sequence,dropped:0,events:this.hideEvents?[]:this.events.slice(0,this.readLimit)}:null}};
    const ack=e.match(/\.ack\((\d+),/);if(ack){if(this.badAck)return {result:{value:Math.max(0,Number(ack[1])-1)}};this.events=this.events.filter(row=>row.source_sequence>Number(ack[1]));return {result:{value:Number(ack[1])}};}
    throw new Error('Unexpected synthetic CDP command');
  }
  async readEvents({afterSequence=0,methods=[],limit=1000}={}){
    if(this.fail)throw new Error('Synthetic disconnect');
    const eligible=this.networkEvents.filter(e=>e.sequence>afterSequence&&methods.includes(e.method));
    const events=eligible.slice(0,limit);const hasMore=eligible.length>events.length;
    const cursor=hasMore?events.at(-1).sequence:this.networkSequence;
    const truncated=this.truncated;this.truncated=false;return {cursor,hasMore,truncated,events};
  }
}
async function withWorkspace(fn){
  const workspace=await fs.mkdtemp(path.join(os.tmpdir(),'brs-cua-timing-'));
  for(const name of ['data_collector/farabi_timing_probe.mjs','scripts/farabi_timing.mjs','scripts/farabi_timing_cua.mjs']){
    const target=path.join(workspace,name);await fs.mkdir(path.dirname(target),{recursive:true});
    await fs.copyFile(new URL('../'+name,import.meta.url),target);
  }
  try{return await fn(workspace);}finally{
    assert.ok(path.resolve(workspace).startsWith(path.resolve(os.tmpdir())+path.sep));
    assert.ok(path.basename(workspace).startsWith('brs-cua-timing-'));
    await fs.rm(workspace,{recursive:true,force:true});
  }
}
test('collector acknowledges only after durable write and finalizes a synthetic report',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('ui.intent',{intent_id:'intent-1',trusted:true,quantity_shares:7,price_rial:100,Authorization:'SEEDED_SECRET'});
  cdp.add('resource',{candidate_intents:['intent-1'],path_class:'orders',host:'127.0.0.1',start_ms:105,response_end_ms:130,duration_ms:25,http_status:201});
  const progress=await r.pump(0);assert.equal(progress.health,'RECORDING');assert.equal(cdp.events.length,0);
  const result=await r.finish();assert.equal(result.health,'SAVED');assert.equal(result.intents,1);
  const persisted=await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8');
  assert.doesNotMatch(persisted,/SEEDED_SECRET|Authorization/);
  const metrics=JSON.parse(await fs.readFile(path.join(result.directory,'metrics.json'),'utf8'));
  assert.equal(metrics.intents[0].exchange_acceptance_time,null);
  assert.ok(cdp.calls.every(call=>['Runtime.evaluate','Network.enable','Page.addScriptToEvaluateOnNewDocument','Page.removeScriptToEvaluateOnNewDocument'].includes(call)));
}));

test('finish drains all page batches and the terminal event before claiming SAVED',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  for(let i=1;i<=1200;i++)cdp.add('ui.intent',{intent_id:`intent-${i}`,trusted:true,quantity_shares:7,price_rial:100});
  const result=await r.finish();
  assert.equal(result.health,'SAVED');assert.equal(result.gaps,0);assert.equal(result.intents,1200);
  const rows=(await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line).record);
  assert.equal(rows.filter(row=>row.type==='ui.intent').length,1200);
  assert.equal(rows.filter(row=>row.type==='session.stop').length,1);
  assert.equal(cdp.events.length,0);
}));

test('an unreadable page backlog is DEGRADED rather than silently complete',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('ui.intent',{intent_id:'intent-1',trusted:true});cdp.hideEvents=true;
  const result=await r.finish();assert.equal(result.health,'DEGRADED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='page_snapshot_no_progress'));
  assert.ok(manifest.gaps.some(g=>g.reason==='session_stop_not_recorded'));
}));

test('an acknowledgement mismatch is retained as DEGRADED',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('ui.intent',{intent_id:'intent-1',trusted:true});cdp.badAck=true;
  assert.equal((await r.pump(0)).health,'DEGRADED');
  const result=await r.finish();assert.equal(result.health,'DEGRADED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='page_ack_mismatch'));
}));

test('finalization does not rearm a missing page probe',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});cdp.probe=false;
  const result=await r.finish();assert.equal(result.health,'DEGRADED');assert.equal(cdp.installCalls,1);
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='stop_probe_unconfirmed'));
  assert.ok(manifest.gaps.some(g=>g.reason==='page_probe_missing'));
}));

test('a bounded page backlog is degraded even if a later pass catches up',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});cdp.readLimit=1;
  for(let i=1;i<=35;i++)cdp.add('ui.intent',{intent_id:`intent-${i}`,trusted:true});
  const result=await r.finish();assert.equal(result.health,'DEGRADED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='page_backlog_limit'));
}));

test('a missing terminal page event prevents SAVED',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  const send=cdp.send.bind(cdp);cdp.send=async(method,params={})=>{
    if(method==='Runtime.evaluate'&&params.expression?.includes('.stop(')){cdp.active=false;return {result:{value:false}};}
    return send(method,params);
  };
  const result=await r.finish();assert.equal(result.health,'DEGRADED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='session_stop_not_recorded'));
}));

test('a probe already stopped by its lifetime limit retains its terminal event',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('session.stop',{reason:'lifetime_limit'});cdp.active=false;
  await r.pump(0);
  const result=await r.finish();assert.equal(result.health,'SAVED');
  const rows=(await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line).record);
  assert.equal(rows.filter(row=>row.type==='session.stop').length,1);
}));

test('a page event beyond its snapshot sequence is DEGRADED',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('ui.intent',{intent_id:'intent-1',trusted:true});
  const send=cdp.send.bind(cdp);cdp.send=async(method,params={})=>{
    const result=await send(method,params);
    if(method==='Runtime.evaluate'&&params.expression?.includes('.read(')&&result.result.value?.events?.length)
      result.result.value.source_sequence--;
    return result;
  };
  const result=await r.finish();assert.equal(result.health,'DEGRADED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='page_source_sequence_gap'));
}));

test('final network poll includes events arriving during page finalization and marks coverage boundary',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.add('ui.intent',{intent_id:'intent-1',trusted:true});
  const send=cdp.send.bind(cdp);let injected=false;
  cdp.send=async(method,params={})=>{
    const result=await send(method,params);
    if(!injected&&method==='Runtime.evaluate'&&params.expression?.includes('.ack(')){
      injected=true;cdp.addNetwork('Network.requestWillBeSent',{requestId:'late-synthetic',timestamp:10,
        request:{url:'http://127.0.0.1:43210/api/orders',method:'GET'}});
    }
    return result;
  };
  const result=await r.finish();assert.equal(result.health,'SAVED');
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.equal(manifest.network_observation.events_after_last_poll,'not_observed');
  assert.equal(manifest.network_observation.tracked_requests_in_flight,1);
  assert.ok(manifest.network_observation.last_poll_collector_wall_utc);
  const rows=(await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line).record);
  assert.equal(rows.filter(row=>row.type==='network.request').length,1);
}));
test('truncation and new document boundary remain DEGRADED through final export',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.truncated=true;assert.equal((await r.pump(0)).health,'DEGRADED');
  cdp.document='page-2000000-1';cdp.sequence=0;cdp.events=[];cdp.add('session.start');
  await r.pump(0);const result=await r.finish();assert.equal(result.health,'DEGRADED');assert.ok(result.gaps>=2);
  const manifest=JSON.parse(await fs.readFile(path.join(result.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='cdp_event_buffer_truncated'));
  assert.ok(manifest.gaps.some(g=>g.reason==='navigation_boundary_missing_tail_possible'));
}));
test('disconnect is explicit and recovery never invents continuity',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  cdp.fail=true;assert.equal((await r.pump(0)).health,'DEGRADED');cdp.fail=false;
  assert.equal((await r.pump(0)).health,'DEGRADED');assert.equal((await r.finish()).health,'DEGRADED');
}));


test('websocket freshness is explicitly unknown until a receive frame is observed and payloads are never stored',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  const initial=(await fs.readFile(path.join(r.directory,'events.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line).record);
  const unknown=initial.find(e=>e.type==='websocket.freshness');
  assert.equal(unknown.data.age_since_collector_observed_ms,null);assert.equal(unknown.data.market_data_freshness,'unknown');
  cdp.addNetwork('Network.webSocketCreated',{requestId:'ws-1',url:'wss://127.0.0.1:43210/tests/farabi-timing'});
  cdp.addNetwork('Network.webSocketFrameReceived',{requestId:'ws-1',timestamp:12.5,response:{payloadData:'SYNTHETIC_PRIVATE_FRAME'}});
  await r.pump(0);const result=await r.finish();
  const saved=await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8');
  assert.doesNotMatch(saved,/SYNTHETIC_PRIVATE_FRAME|payloadData/);
  assert.match(saved,/websocket\.freshness/);assert.match(saved,/age_since_collector_observed_ms/);
}));

test('duplicate websocket creation at capacity does not evict another connection',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  for(let i=0;i<100;i++)cdp.addNetwork('Network.webSocketCreated',{requestId:`ws-${i}`,url:'wss://127.0.0.1/socket'});
  cdp.addNetwork('Network.webSocketCreated',{requestId:'ws-99',url:'wss://127.0.0.1/socket'});
  assert.equal((await r.pump(0)).health,'RECORDING');assert.equal(r.manifest.gaps.length,0);
  assert.equal((await r.finish()).health,'SAVED');
}));

test('request-map overflow becomes an explicit degraded gap',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),r=await startTiming(cdp,{workspace,synthetic:true});
  for(let i=0;i<10001;i++)cdp.addNetwork('Network.requestWillBeSent',{requestId:`req-${i}`,timestamp:10+i,
    request:{url:'http://127.0.0.1:43210/api/orders',method:'GET'}});
  const progress=await r.pump(0);assert.equal(progress.health,'DEGRADED');
  const result=await r.finish();const saved=await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8');
  assert.match(saved,/network_request_tracking_capacity_exceeded/);assert.equal(result.health,'DEGRADED');
}));

test('unsupported reload hook still records and poll rearm preserves the missing interval',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp();cdp.hook=false;
  const r=await startTiming(cdp,{workspace,synthetic:true});
  assert.equal(r.manifest.capabilities.automatic_new_document_hook,false);
  assert.equal((await r.pump(0)).health,'RECORDING');
  cdp.probe=false;cdp.document='page-3000000-1';cdp.sequence=0;cdp.events=[];
  const recovered=await r.pump(0);assert.equal(recovered.probe_present,true);
  assert.equal(recovered.health,'DEGRADED');
  const result=await r.finish();
  const saved=await fs.readFile(path.join(result.directory,'events.jsonl'),'utf8');
  assert.match(saved,/collector.rearm/);assert.match(saved,/missing_interval/);
  assert.ok(r.manifest.gaps.some(g=>g.reason==='page_probe_missing'));
}));

test('single-call window rejects invalid duration before accessing CDP',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp();
  for(const durationSeconds of [NaN,Infinity,0,45.01])
    await assert.rejects(captureTimingWindow(cdp,{workspace,synthetic:true,durationSeconds}));
  assert.deepEqual(cdp.calls,[]);
}));

test('single-call window saves and stops once before returning its receipt',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp();
  const receipt=await captureTimingWindow(cdp,{workspace,synthetic:true,durationSeconds:0.1});
  assert.equal(receipt.health,'SAVED');assert.equal(receipt.gaps,0);assert.equal(receipt.pump_failed,false);
  const manifest=JSON.parse(await fs.readFile(path.join(receipt.directory,'manifest.json'),'utf8'));
  assert.equal(manifest.collector_driver.mode,'single_awaited_tool_call');assert.equal(manifest.journal_chain_valid,true);
  const records=(await fs.readFile(path.join(receipt.directory,'events.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line).record);
  assert.equal(records.filter(r=>r.type==='collector.stop').length,1);assert.equal(cdp.active,false);
}));

test('single-call window retains a truncated CDP interval as DEGRADED',async()=>withWorkspace(async workspace=>{
  const cdp=new FakeCdp(),read=cdp.readEvents.bind(cdp);let calls=0;
  cdp.readEvents=async options=>{if(++calls===3)cdp.truncated=true;return read(options);};
  const receipt=await captureTimingWindow(cdp,{workspace,synthetic:true,durationSeconds:0.1});
  assert.equal(receipt.health,'DEGRADED');assert.ok(receipt.gaps>0);assert.equal(cdp.active,false);
  const manifest=JSON.parse(await fs.readFile(path.join(receipt.directory,'manifest.json'),'utf8'));
  assert.ok(manifest.gaps.some(g=>g.reason==='cdp_event_buffer_truncated'));
}));
