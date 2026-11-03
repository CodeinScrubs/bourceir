import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {NetworkFilter,targetClass,createJournal,writeReport,analyzeEvents} from '../scripts/farabi_timing.mjs';
import {installFarabiTiming} from '../data_collector/farabi_timing_probe.mjs';

test('network metadata drops seeded auth, body, query, path identifiers and unrelated traffic',()=>{
  const filter=new NetworkFilter();
  const request=filter.event({method:'Network.requestWillBeSent',params:{requestId:'secret-account-id',timestamp:100,
    wallTime:200,request:{url:'https://gateway.farabixo.com/api/v2/orders/private-id?token=SEEDED_TOKEN',method:'POST',
      headers:{Authorization:'Bearer SEEDED_TOKEN',Cookie:'SEEDED_COOKIE'},postData:'SEEDED_PASSWORD'}}});
  assert.equal(request.data.path_class,'order_detail');assert.equal(request.data.method,'POST');
  assert.equal(request.data.alias,'request-1');
  assert.doesNotMatch(JSON.stringify(request),/SEEDED|secret-account|private-id|Authorization|Cookie/);
  assert.equal(filter.event({method:'Network.requestWillBeSent',params:{requestId:'other',request:{url:'https://evil.example/orders',method:'POST'}}}),null);
});
test('unknown timing stays null and zero/negative protocol fields are retained honestly',()=>{
  const f=new NetworkFilter();f.event({method:'Network.requestWillBeSent',params:{requestId:'a',request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'POST'}}});
  const response=f.event({method:'Network.responseReceived',params:{requestId:'a',timestamp:2,
    response:{status:201,protocol:'h2',headers:{'set-cookie':'SECRET'},timing:{dnsStart:-1,sendStart:0,sendEnd:1}}}});
  assert.equal(response.data.timing.dnsStart,-1);assert.equal(response.data.timing.sendStart,0);
  assert.equal(response.data.timing.receiveHeadersStart,null);assert.doesNotMatch(JSON.stringify(response),/SECRET/);
});
test('failure errors are reduced to a fixed network class',()=>{
  const f=new NetworkFilter();f.event({method:'Network.requestWillBeSent',params:{requestId:'a',request:{url:'https://gateway.farabixo.com/api/v2/orders'}}});
  const r=f.event({method:'Network.loadingFailed',params:{requestId:'a',errorText:'net::ERR_TIMED_OUT token=SECRET'}});
  assert.equal(r.data.error_class,'net::ERR_TIMED_OUT');assert.doesNotMatch(JSON.stringify(r),/SECRET/);
});
test('duplicate requests preserve redirect hops and independent requests',()=>{
  const f=new NetworkFilter(),evt=id=>({method:'Network.requestWillBeSent',params:{requestId:id,request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'POST'}}});
  const a=f.event(evt('a')),b=f.event(evt('b')),redirect=f.event(evt('a'));
  assert.notEqual(a.data.alias,b.data.alias);assert.equal(redirect.data.hop,1);assert.equal(redirect.data.alias,a.data.alias);
});
test('only exact broker hosts are admitted; simulator exception is explicit',()=>{
  assert.equal(targetClass('https://gateway.farabixo.com.evil.test/orders'),null);
  assert.equal(targetClass('http://127.0.0.1/api/orders'),null);
  assert.equal(targetClass('http://127.0.0.1/api/orders',true).path_class,'orders');
});
test('unknown acceptance and ambiguous intents are never upgraded from HTTP 201',()=>{
  const intent=id=>({record:{type:'ui.intent',document_id:'d',page_ms:100,data:{intent_id:id,quantity_shares:7,price_rial:100,event_timestamp_ms:95}}});
  const rows=[intent('a'),intent('b'),{record:{type:'resource',document_id:'d',data:{candidate_intents:['a','b'],
    path_class:'orders',start_ms:120,response_end_ms:300,duration_ms:180,http_status:201,method:null}}}];
  const result=analyzeEvents(rows);assert.equal(result.intents.length,2);
  for(const i of result.intents){assert.equal(i.exchange_acceptance_time,null);assert.equal(i.queue_rank,null);assert.equal(i.correlation,'time_window_inference');
    assert.equal(i.click_timestamp_basis,'event.timeStamp');assert.equal(i.candidates[0].click_to_resource_start_ms,25);}
  assert.equal(result.guaranteed_queue_priority,false);
});

test('network summary computes browser request duration from CDP monotonic timestamps only',()=>{
  const f=new NetworkFilter();
  const request=f.event({method:'Network.requestWillBeSent',params:{requestId:'r1',timestamp:10,
    wallTime:200,request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'POST'}}});
  const response=f.event({method:'Network.responseReceived',params:{requestId:'r1',timestamp:10.2,
    response:{status:201,protocol:'h2',connectionReused:true,timing:{requestTime:10}}}});
  const finished=f.event({method:'Network.loadingFinished',params:{requestId:'r1',timestamp:10.5,encodedDataLength:42}});
  const result=analyzeEvents([request,response,finished].map(record=>({record})));
  assert.equal(result.network_summary.requests_observed,1);
  assert.equal(result.network_summary.completed,1);
  assert.equal(result.network_summary.failed,0);
  assert.equal(result.network_request_samples[0].http_status,201);
  assert.equal(result.network_request_samples[0].duration_ms,500);
  assert.equal(result.exchange_acceptance_times_available,0);
  assert.match(result.network_summary.note,/not one-way network latency or exchange acceptance time/);
});

test('network request tracking overflow is counted rather than silently hidden',()=>{
  const f=new NetworkFilter();
  for(let i=0;i<10001;i++)f.event({method:'Network.requestWillBeSent',params:{requestId:`r${i}`,timestamp:i,
    request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'GET'}}});
  assert.equal(f.evictions,1);
  assert.equal(f.requests.size,10000);
});

test('redirect at full tracking capacity keeps existing correlation without evicting another request',()=>{
  const f=new NetworkFilter();
  for(let i=0;i<10000;i++)f.event({method:'Network.requestWillBeSent',params:{requestId:`r${i}`,timestamp:i,
    request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'GET'}}});
  const prior=f.requests.get('r9999');
  const redirected=f.event({method:'Network.requestWillBeSent',params:{requestId:'r9999',timestamp:10001,
    request:{url:'https://gateway.farabixo.com/api/v2/orders',method:'GET'}}});
  assert.equal(f.evictions,0);assert.equal(f.requests.size,10000);assert.ok(f.requests.has('r0'));
  assert.equal(redirected.data.alias,prior.alias);assert.equal(redirected.data.hop,1);
});

test('missing resource times stay unknown instead of becoming negative durations',()=>{
  const rows=[{record:{type:'ui.intent',document_id:'d',page_ms:100,data:{intent_id:'a',event_timestamp_ms:95}}},
    {record:{type:'resource',document_id:'d',data:{candidate_intents:['a'],path_class:'orders',start_ms:null,response_end_ms:null,duration_ms:null}}}];
  const i=analyzeEvents(rows).intents[0];
  assert.equal(i.candidates[0].click_to_resource_start_ms,null);
  assert.equal(i.candidates[0].click_to_response_end_ms,null);
  assert.equal(i.candidates[0].resource_duration_ms,null);
  assert.equal(i.cdp_request_association,'not_established');
});

test('future event timestamp falls back to the observer and cannot invent an earlier click',()=>{
  const rows=[{record:{type:'ui.intent',document_id:'d',page_ms:100,data:{intent_id:'a',event_timestamp_ms:500}}},
    {record:{type:'resource',document_id:'d',data:{candidate_intents:['a'],path_class:'orders',start_ms:120,response_end_ms:130,duration_ms:10}}}];
  const i=analyzeEvents(rows).intents[0];
  assert.equal(i.click_timestamp_basis,'observer_page_ms');assert.equal(i.candidates[0].click_to_resource_start_ms,20);
});

test('mixed or missing network clock labels never yield a CDP duration',()=>{
  const result=analyzeEvents([
    {record:{type:'network.request',clock:'page_performance_ms',source_time:10,data:{alias:'a',hop:0,method:'GET'}}},
    {record:{type:'network.finished',clock:'cdp_monotonic_seconds',source_time:10.5,data:{alias:'a',hop:0}}}
  ]);
  assert.equal(result.network_request_samples[0].duration_ms,null);
  assert.equal(result.network_summary.requests_observed,1);
  assert.equal(result.network_summary.incomplete_duration,1);
});


test('collector entry points contain no programmatic trading submit, replay, or cancel actions',async()=>{
  const source=(await fs.readFile(new URL('../scripts/farabi_timing_cua.mjs',import.meta.url),'utf8'))+
    (await fs.readFile(new URL('../data_collector/farabi_timing_probe.mjs',import.meta.url),'utf8'));
  assert.doesNotMatch(source,/Input\.dispatchMouseEvent|Input\.insertText|Network\.continueInterceptedRequest|Network\.setExtraHTTPHeaders|\.requestSubmit\s*\(|\.click\s*\(|\.submit\s*\(|fetch\s*\(|new XMLHttpRequest|new WebSocket/);
});

test('journal fsync, chain, report, and tamper detection use only temporary synthetic storage',async()=>{
  const temp=await fs.mkdtemp(path.join(os.tmpdir(),'brs-timing-test-'));
  try{
    const out=path.join(temp,'session');const j=await createJournal(out,{synthetic:true,fixtureRoot:temp});
    await j.append([{type:'collector.start',data:{}}]);assert.equal(j.durableSequence,1);await j.close();
    await writeReport(out,{version:'1.0.2',synthetic:true,health:'SAVED',gaps:[]});
    const oldManifest=JSON.parse(await fs.readFile(path.join(out,'manifest.json'),'utf8'));
    assert.equal(oldManifest.version,'1.0.2','re-analysis must preserve the supplied capture version');
    const manifest=JSON.parse(await fs.readFile(path.join(out,'manifest.json'),'utf8'));
    assert.equal(manifest.journal_chain_valid,true);assert.equal(manifest.last_durable_ingest_sequence,1);
    assert.match(await fs.readFile(path.join(out,'report.md'),'utf8'),/SYNTHETIC TEST/);
    const file=path.join(out,'events.jsonl'),raw=await fs.readFile(file,'utf8');
    await fs.writeFile(file,raw.replace('collector.start','collector.changed'));
    await assert.rejects(writeReport(out,{synthetic:true}),/integrity/);
  }finally{
    assert.ok(path.resolve(temp).startsWith(path.resolve(os.tmpdir())+path.sep));
    assert.ok(path.basename(temp).startsWith('brs-timing-test-'));
    await fs.rm(temp,{recursive:true,force:true});
  }
});
test('journal refuses output outside private/test root',async()=>{
  await assert.rejects(createJournal(path.resolve('README.md'),{privateRoot:path.resolve('data/private/farabi-timing')}),/approved/);
});
test('probe source contains no active trade or auth APIs',()=>{
  const source=installFarabiTiming.toString();
  assert.doesNotMatch(source,/\b(fetch|XMLHttpRequest|WebSocket)\s*\(|\.click\s*\(|\.submit\s*\(|\.requestSubmit\s*\(|document\.cookie|localStorage|sessionStorage|Authorization|postData/);
  assert.match(source,/passive:true/);assert.match(source,/page_ring|dropped/);
});
