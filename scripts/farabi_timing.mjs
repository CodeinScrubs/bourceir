/** Passive timing data contract, privacy filter, durable local journal, and report. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

export const timingVersion='1.0.3';
const hosts=new Set(['farabixo.irfarabi.com','gateway.farabixo.com']);
const finite=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
const safeWord=v=>typeof v==='string'&&/^[A-Za-z0-9_:. -]{1,100}$/.test(v)?v:null;
const bool=v=>typeof v==='boolean'?v:null;
const choose=(v,values)=>values.includes(v)?v:null;
export function sanitizePageEvent(raw){
  const doc=typeof raw.document_id==='string'&&/^page-\d+-\d+$/.test(raw.document_id)?raw.document_id:null;
  if(!doc||!Number.isSafeInteger(raw.source_sequence)||raw.source_sequence<1)return null;
  const data=raw.data||{},out={source_sequence:raw.source_sequence,document_id:doc,clock_segment:finite(raw.clock_segment),
    page_ms:finite(raw.page_ms),device_wall_ms:finite(raw.device_wall_ms),type:raw.type,synthetic:raw.synthetic===true};
  const form=d=>({isin:/^[A-Z0-9]{12}$/.test(d.isin||'')?d.isin:null,side:choose(d.side,['buy','sell']),ordinary:bool(d.ordinary),
    quantity_shares:finite(d.quantity_shares),price_rial:finite(d.price_rial),day_validity:bool(d.day_validity),
    submit_enabled:bool(d.submit_enabled),available:bool(d.available)});
  const id=v=>/^intent-\d+$/.test(v||'')?v:null;
  if(raw.type==='ui.intent')out.data={...form(data),intent_id:id(data.intent_id),event_timestamp_ms:finite(data.event_timestamp_ms),trusted:bool(data.trusted),action:'submit_control'};
  else if(raw.type==='ui.form')out.data=form(data);
  else if(raw.type==='resource')out.data={host:choose(data.host,[...hosts,'127.0.0.1']),path_class:choose(data.path_class,['orders','order_detail','market_data']),
    start_ms:finite(data.start_ms),duration_ms:finite(data.duration_ms),response_end_ms:finite(data.response_end_ms),
    request_start_ms:finite(data.request_start_ms),response_start_ms:finite(data.response_start_ms),http_status:finite(data.http_status),
    initiator:choose(data.initiator,['xmlhttprequest','fetch','script','link','other']),
    candidate_intents:Array.isArray(data.candidate_intents)?data.candidate_intents.map(id).filter(Boolean).slice(0,200):[],
    correlation:'time_window_inference',method:null};
  else if(raw.type==='ui.orders')out.data={available:bool(data.available),visible_rows_only:true,exchange_acceptance_time:null,queue_rank:null,
    rows:Array.isArray(data.rows)?data.rows.slice(0,50).map(row=>({side:choose(row.side,['buy','sell']),quantity_shares:finite(row.quantity_shares),
      filled_shares:finite(row.filled_shares),price_rial:finite(row.price_rial),broker_state:choose(row.broker_state,
        ['queued','filled','cancelled','failed','expired','sending','partially_filled','unknown']),displayed_time:null,
      displayed_time_semantics:'not_captured'})):[]};
  else if(raw.type==='heartbeat')out.data={health:choose(data.health,['RECORDING','DEGRADED']),writer_ack_age_ms:finite(data.writer_ack_age_ms),
    last_durable_page_sequence:finite(data.last_durable_page_sequence),buffered:finite(data.buffered),dropped:finite(data.dropped),
    visibility:choose(data.visibility,['visible','hidden']),scope_ok:bool(data.scope_ok),form_detected:bool(data.form_detected),
    event_loop_delay_ms:finite(data.event_loop_delay_ms),wall_vs_origin_ms:finite(data.wall_vs_origin_ms)};
  else if(raw.type==='clock.discontinuity')out.data={wall_delta_ms:finite(data.wall_delta_ms),monotonic_delta_ms:finite(data.monotonic_delta_ms),
    uncertainty_ms:null,reason:choose(data.reason,['suspend_or_scheduler_gap','wall_step'])};
  else if(raw.type==='page.visibility')out.data={visibility:choose(data.visibility,['visible','hidden'])};
  else if(raw.type==='page.boundary')out.data={reason:'pagehide',missing_future_possible:true};
  else if(raw.type==='session.start')out.data={missing_prefix:true,scope:'selected_tab_top_frame',utc_uncertainty_ms:null};
  else if(raw.type==='session.stop')out.data={reason:choose(data.reason,['lifetime_limit','operator_stop'])};
  else return null;
  return out;
}
export function targetClass(value,synthetic=false){
  let u;try{u=new URL(value);}catch{return null;}
  if(!hosts.has(u.hostname)&&!(synthetic&&u.hostname==='127.0.0.1'))return null;
  const p=u.pathname;
  return {host:u.hostname,path_class:/\/orders?\/?$/i.test(p)?'orders':/\/orders?\//i.test(p)?'order_detail':
    /stockwatch|instruments|calendar/i.test(p)?'market_data':'other_broker',query_captured:false};
}
export class NetworkFilter{
  constructor({synthetic=false}={}){this.synthetic=synthetic;this.requests=new Map();this.count=0;this.evictions=0;}
  event(event){
    const p=event.params||{},method=event.method;
    if(method==='Network.requestWillBeSent'){
      const target=targetClass(p.request?.url,this.synthetic);if(!target)return null;
      const previous=this.requests.get(p.requestId),hop=previous?previous.hop+1:0;
      if(!previous&&this.requests.size>=10000){this.requests.delete(this.requests.keys().next().value);this.evictions++;}
      const state={alias:previous?.alias||`request-${++this.count}`,hop,...target};this.requests.set(p.requestId,state);
      return {type:'network.request',clock:'cdp_monotonic_seconds',source_time:finite(p.timestamp),
        data:{...state,method:['GET','POST','PUT','PATCH','DELETE','OPTIONS','HEAD'].includes(p.request?.method)?p.request.method:'OTHER',
          wall_time_seconds:finite(p.wallTime),resource_type:safeWord(p.type),redirect_hop:hop}};
    }
    const state=this.requests.get(p.requestId);
    if(!state)return null;
    if(method==='Network.responseReceived'){
      const response=p.response||{},t=response.timing||{};
      const timing={};for(const key of ['requestTime','proxyStart','proxyEnd','dnsStart','dnsEnd','connectStart','connectEnd',
        'sslStart','sslEnd','sendStart','sendEnd','receiveHeadersStart','receiveHeadersEnd'])timing[key]=finite(t[key]);
      return {type:'network.response',clock:'cdp_monotonic_seconds',source_time:finite(p.timestamp),data:{...state,
        http_status:finite(response.status),protocol:choose(response.protocol,['h2','h3','http/1.0','http/1.1']),connection_reused:typeof response.connectionReused==='boolean'?response.connectionReused:null,
        from_cache:response.fromDiskCache===true,from_service_worker:response.fromServiceWorker===true,timing}};
    }
    if(method==='Network.loadingFinished'||method==='Network.loadingFailed'){
      this.requests.delete(p.requestId);
      return {type:method.endsWith('Failed')?'network.failed':'network.finished',clock:'cdp_monotonic_seconds',source_time:finite(p.timestamp),
        data:{...state,encoded_bytes:finite(p.encodedDataLength),canceled:p.canceled===true,
          error_class:typeof p.errorText==='string'?p.errorText.match(/net::ERR_[A-Z_]+/)?.[0]||'network_error':null}};
    }
    return null;
  }
}
export async function createJournal(directory,{synthetic=false,privateRoot,fixtureRoot}={}){
  const root=path.resolve(synthetic?fixtureRoot||'tests/.timing-output':privateRoot||'data/private/farabi-timing');
  const out=path.resolve(directory);
  if(out===root||!out.startsWith(root+path.sep))throw new Error('Session must be inside the approved private/test root');
  await fs.mkdir(out,{recursive:true});
  const file=await fs.open(path.join(out,'events.jsonl'),'wx');let sequence=0,previous='0'.repeat(64),failed=false;
  const api={directory:out,synthetic,get durableSequence(){return sequence;},get failed(){return failed;},
    async append(records){
      if(failed)throw new Error('Writer failed; session incomplete');
      const lines=[];
      for(const record of records){
        const row={schema_version:timingVersion,synthetic,ingest_sequence:++sequence,
          received_wall_utc:new Date().toISOString(),received_monotonic_ms:performance.now(),
          collector_clock:'collector_performance_ms',
          previous_sha256:previous,record};
        const encoded=JSON.stringify(row);previous=crypto.createHash('sha256').update(encoded).digest('hex');
        lines.push(JSON.stringify({...row,sha256:previous}));
      }
      try{if(lines.length){await file.writeFile(lines.join('\n')+'\n');await file.sync();}}
      catch{failed=true;throw new Error('Local writer failed; session incomplete');}
      return sequence;
    },
    async close(){await file.sync();await file.close();},
  };return api;
}
export function analyzeEvents(rows){
  const records=rows.map(row=>row.record),intents=records.filter(r=>r.type==='ui.intent');
  const networkByHop=new Map();
  for(const record of records){
    if(!['network.request','network.response','network.finished','network.failed'].includes(record.type))continue;
    const d=record.data||{};
    if(typeof d.alias!=='string'||!Number.isSafeInteger(d.hop))continue;
    const key=`${d.alias}:${d.hop}`;
    const item=networkByHop.get(key)||{alias:d.alias,hop:d.hop,host:d.host??null,path_class:d.path_class??null,
      method:null,http_status:null,protocol:null,outcome:'incomplete',start_source_time:null,end_source_time:null,
      duration_ms:null,encoded_bytes:null,error_class:null,request_start_observed:false};
    const sourceTime=record.clock==='cdp_monotonic_seconds'?finite(record.source_time):null;
    if(record.type==='network.request'){
      item.request_start_observed=true;item.start_source_time=sourceTime;item.method=d.method??null;
      item.host=d.host??item.host;item.path_class=d.path_class??item.path_class;
    }else if(record.type==='network.response'){
      item.http_status=finite(d.http_status);item.protocol=d.protocol??null;
    }else if(record.type==='network.finished'||record.type==='network.failed'){
      item.end_source_time=sourceTime;item.outcome=record.type==='network.finished'?'finished':'failed';
      item.encoded_bytes=finite(d.encoded_bytes);item.error_class=d.error_class??null;
    }
    if(item.start_source_time!==null&&item.end_source_time!==null&&item.end_source_time>=item.start_source_time)
      item.duration_ms=(item.end_source_time-item.start_source_time)*1000;
    networkByHop.set(key,item);
  }
  const networkRequests=[...networkByHop.values()];
  const durations=networkRequests.map(r=>r.duration_ms).filter(v=>v!==null&&Number.isFinite(v)).sort((a,b)=>a-b);
  const quantile=p=>durations.length?durations[Math.max(0,Math.ceil(p*durations.length)-1)]:null;
  const networkSummary={clock:'cdp_monotonic_seconds',requests_observed:networkRequests.filter(r=>r.request_start_observed).length,
    completed:networkRequests.filter(r=>r.outcome==='finished'&&r.duration_ms!==null).length,
    failed:networkRequests.filter(r=>r.outcome==='failed').length,
    incomplete_duration:networkRequests.filter(r=>r.duration_ms===null).length,
    duration_ms:durations.length?{min:durations[0],median:durations.length%2?durations[(durations.length-1)/2]:(durations[durations.length/2-1]+durations[durations.length/2])/2,
      p95:quantile(.95),max:durations.at(-1)}:{min:null,median:null,p95:null,max:null},
    note:'Durations use CDP monotonic event timestamps for observed browser requests. They are not one-way network latency or exchange acceptance time.'};
  return {intents:intents.map(intent=>{
    const observedPageMs=finite(intent.page_ms),eventPageMs=finite(intent.data.event_timestamp_ms);
    const useEventTimestamp=eventPageMs!==null&&eventPageMs>=0&&observedPageMs!==null&&
      eventPageMs<=observedPageMs&&observedPageMs-eventPageMs<=1000;
    const clickPageMs=useEventTimestamp?eventPageMs:observedPageMs;
    const candidates=records.filter(r=>r.type==='resource'&&r.document_id===intent.document_id&&
      r.data.candidate_intents?.includes(intent.data.intent_id)&&r.data.path_class==='orders');
    return {intent_id:intent.data.intent_id,clock:'page_performance_ms',document_id:intent.document_id,
      quantity_shares:intent.data.quantity_shares,price_rial:intent.data.price_rial,
      click_timestamp_basis:useEventTimestamp?'event.timeStamp':'observer_page_ms',
      cdp_request_association:'not_established',
      resource_association:candidates.length===0?'unknown':candidates.length>1||
        candidates.some(r=>r.data.candidate_intents?.length>1)?'ambiguous':'single_time_window_candidate_inferred',
      exchange_acceptance_time:null,queue_rank:null,correlation:'time_window_inference',
      candidates:candidates.map(r=>({http_status:r.data.http_status,method:r.data.method,
        click_to_resource_start_ms:clickPageMs===null||finite(r.data.start_ms)===null?null:r.data.start_ms-clickPageMs,
        resource_duration_ms:finite(r.data.duration_ms),
        click_to_response_end_ms:clickPageMs===null||finite(r.data.response_end_ms)===null?null:r.data.response_end_ms-clickPageMs}))};
  }),network_summary:networkSummary,network_request_samples:networkRequests.map(({alias,hop,host,path_class,method,http_status,protocol,outcome,duration_ms,encoded_bytes,error_class})=>
    ({alias,hop,host,path_class,method,http_status,protocol,outcome,duration_ms,encoded_bytes,error_class})),
    exchange_acceptance_times_available:0,guaranteed_queue_priority:false};
}
export async function writeReport(directory,manifest){
  const data=await fs.readFile(path.join(directory,'events.jsonl'),'utf8');
  const lines=data.trim()?data.trim().split('\n'):[];let previous='0'.repeat(64),expected=1;
  const rows=lines.map(line=>{const row=JSON.parse(line),{sha256,...content}=row;
    if(content.ingest_sequence!==expected++||content.previous_sha256!==previous||
      crypto.createHash('sha256').update(JSON.stringify(content)).digest('hex')!==sha256)throw new Error('Journal integrity failure');
    previous=sha256;return row;});
  const metrics=analyzeEvents(rows);
  const anchors=rows.filter(row=>['clock.anchor','clock.discontinuity'].includes(row.record.type));
  await fs.writeFile(path.join(directory,'clock_anchors.jsonl'),anchors.map(row=>JSON.stringify(row)).join('\n')+(anchors.length?'\n':''));
  await fs.writeFile(path.join(directory,'metrics.json'),JSON.stringify(metrics,null,2));
  await fs.writeFile(path.join(directory,'manifest.json'),JSON.stringify({...manifest,version:manifest.version||timingVersion,
    last_durable_ingest_sequence:rows.length,journal_chain_valid:true,final_journal_sha256:previous},null,2));
  const report=[`# ${manifest.synthetic?'SYNTHETIC TEST':'Farabi passive observation'}`,'',
    `Capture health: ${manifest.health}. Missing prefix: true.`,
    `Recorded events: ${rows.length}. Manual intents: ${metrics.intents.length}.`,
    `Gaps: ${JSON.stringify(manifest.gaps||[])}.`,
    'Exchange acceptance timestamp and queue rank: UNKNOWN.','',
    'Page-resource click correlations are inferred within a time window; they are not proof of causation.',
    `Observed CDP requests: ${metrics.network_summary.requests_observed}; completed with duration: ${metrics.network_summary.completed}; failed: ${metrics.network_summary.failed}; without duration: ${metrics.network_summary.incomplete_duration}.`,
    `CDP monotonic duration ms (min/median/p95/max): ${JSON.stringify(metrics.network_summary.duration_ms)}.`,
    'Browser durations are not one-way latency or exchange acceptance time.',
    ...metrics.intents.map(i=>`- ${i.intent_id}: ${JSON.stringify(i.candidates)}`),'',
    'No order submission, replay, retry, cancel, or replace is implemented.'];
  await fs.writeFile(path.join(directory,'report.md'),report.join('\n')+'\n');
  const names=(await fs.readdir(directory)).filter(n=>n!=='SHA256SUMS.txt');const hashes=[];
  for(const name of names){const entry=path.join(directory,name);if((await fs.stat(entry)).isFile())hashes.push(`${crypto.createHash('sha256').update(await fs.readFile(entry)).digest('hex')}  ${name}`);}
  await fs.writeFile(path.join(directory,'SHA256SUMS.txt'),hashes.join('\n')+'\n');return metrics;
}
if(typeof process!=='undefined'&&process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const directory=process.argv[2];if(!directory)throw new Error('Provide a saved session folder');
  const manifest=JSON.parse(await fs.readFile(path.join(directory,'manifest.json'),'utf8'));
  const metrics=await writeReport(directory,manifest);console.log(JSON.stringify({intents:metrics.intents.length,report:path.join(directory,'report.md')}));
}
