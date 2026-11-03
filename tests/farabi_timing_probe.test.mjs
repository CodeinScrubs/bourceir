/** Synthetic VM tests, not browser/rendering or broker evidence. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {installFarabiTiming} from '../data_collector/farabi_timing_probe.mjs';
import {sanitizePageEvent} from '../scripts/farabi_timing.mjs';

function fixture({host='farabixo.irfarabi.com',capacity=2048,rowState=null,formPresent=true}={}){
  let now=1000,wallStep=0;const listeners=new Map(),intervals=new Map(),timeouts=new Map(),observers=[];let id=0;
  const rect=()=>({width:100,height:20});
  const fields=[{value:'7'},{value:'100'}];
  const form={innerText:'معتبر تا امروز',querySelectorAll:()=>fields};
  const button={innerText:'ارسال خرید',disabled:false,getBoundingClientRect:rect,closest:selector=>selector==='button'?button:form};
  const tabs=['خرید','سفارش عادی'].map(innerText=>({innerText}));
  const body={appendChild(){}};
  const row={getBoundingClientRect:rect,querySelectorAll:()=>['خرید','فکور','7 (0)','100','100','synthetic',rowState].map(innerText=>({innerText}))};
  const table={querySelectorAll:()=>[row]};
  const heading={innerText:'وضعیت سفارش',getBoundingClientRect:rect,closest:()=>table};
  const document={body,visibilityState:'visible',querySelectorAll(selector){
    if(selector==='button')return formPresent?[button]:[];if(selector.startsWith('[role="tab"]'))return tabs;
    if(selector.startsWith('th,')&&rowState!==null)return [heading];return [];
  },createElement:()=>({style:{},setAttribute(){},textContent:''}),
  addEventListener:(type,fn)=>listeners.set(type,fn),removeEventListener:type=>listeners.delete(type)};
  const window={addEventListener:(type,fn)=>listeners.set(type,fn),removeEventListener:type=>listeners.delete(type)};
  class FakeObserver{constructor(callback){this.callback=callback;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}}
  const scope={window,document,location:new URL(`https://${host}/?isin=IRO3FSTZ0001`),URL,Map,Set,
    performance:{now:()=>now,timeOrigin:1000000},Date:class extends Date{static now(){return 1000000+now+wallStep;}},
    PerformanceObserver:FakeObserver,setInterval:fn=>{intervals.set(++id,fn);return id;},
    setTimeout:fn=>{timeouts.set(++id,fn);return id;},clearInterval:key=>{intervals.delete(key);timeouts.delete(key);}};
  const result=vm.runInNewContext(`(${installFarabiTiming.toString()})(${JSON.stringify({isin:'IRO3FSTZ0001',symbol:'فکور',capacity})})`,scope);
  return {result,api:()=>window.__brsFarabiTimingV1,intervals,observers,listeners,
    reinstall(){return vm.runInNewContext(`(${installFarabiTiming.toString()})(${JSON.stringify({isin:'IRO3FSTZ0001',symbol:'فکور',capacity})})`,scope);},
    click(trusted=true){listeners.get('click')?.({isTrusted:trusted,eventTimeStamp:now-1,timeStamp:now-1,target:button});},
    advance(ms,step=0){now+=ms;wallStep+=step;for(const fn of intervals.values())fn();},
    resource({start=now,duration=30,status=201,url='https://gateway.farabixo.com/api/v2/orders?token=SEEDED'}={}){
      observers[0].callback({getEntries:()=>[{name:url,startTime:start,duration,responseEnd:start+duration,
        requestStart:0,responseStart:0,responseStatus:status,initiatorType:'xmlhttprequest'}]});
    }};
}
test('unrelated page cannot install probe',()=>{const f=fixture({host:'other.example'});assert.equal(f.result.installed,false);assert.equal(f.api(),undefined);});

test('an active prior probe is refused without overwriting its pending events',()=>{
  const f=fixture();f.click();const before=f.api().read().source_sequence;
  const next=f.reinstall();assert.equal(next.installed,false);assert.equal(next.reason,'active_probe_already_exists');
  assert.equal(f.api().read().source_sequence,before);assert.equal(f.observers.length,1);f.api().stop();
});
test('real mode ignores untrusted events; identical trusted intents stay distinct',()=>{
  const f=fixture();f.click(false);f.click(true);f.click(true);
  const intents=f.api().read().events.filter(e=>e.type==='ui.intent');assert.equal(intents.length,2);
  assert.notEqual(intents[0].data.intent_id,intents[1].data.intent_id);f.api().stop();
});
test('events remain buffered until durability ack; stale writer and overflow remain visible',()=>{
  const f=fixture({capacity:32});f.api().ack(0,true);f.advance(1000);
  assert.equal(f.api().read().events.filter(e=>e.type==='heartbeat').at(-1).data.health,'RECORDING');
  f.advance(9000);assert.equal(f.api().read().events.filter(e=>e.type==='heartbeat').at(-1).data.health,'DEGRADED');
  for(let i=0;i<45;i++)f.click();
  const snapshot=f.api().read();assert.ok(snapshot.dropped>0);assert.equal(snapshot.events.length,32);
  assert.ok(snapshot.events[0].source_sequence>1);
  f.api().ack(snapshot.source_sequence,true);assert.equal(f.api().read().events.length,0);f.api().stop();
});
test('clock step and scheduler gap create new explicit segments',()=>{
  const f=fixture();f.advance(1000,500);f.advance(7000);
  const changes=f.api().read().events.filter(e=>e.type==='clock.discontinuity');assert.equal(changes.length,2);
  assert.equal(changes[0].clock_segment,2);assert.equal(changes[1].clock_segment,3);f.api().stop();
});

test('a clock step remains DEGRADED after a fresh writer ack',()=>{
  const f=fixture();f.api().ack(0,true);f.advance(1000,500);
  f.api().ack(f.api().read().source_sequence,true);f.advance(1000);
  const beat=f.api().read().events.filter(e=>e.type==='heartbeat').at(-1);
  assert.equal(beat.data.health,'DEGRADED');f.api().stop();
});

test('broker withdrawal and multiline order badges are parsed without inventing execution',()=>{
  for(const [label,expected] of [['انصراف','cancelled'],['لغو شده\nشبانه‌روزی','cancelled'],['سفارش ویرایش شده و در صف','queued']]){
    const f=fixture({rowState:label});
    const record=f.api().read().events.find(e=>e.type==='ui.orders');
    assert.equal(record.data.rows[0].broker_state,expected);
    assert.equal(record.data.rows[0].filled_shares,0);
    assert.equal(record.data.queue_rank,null);f.api().stop();
  }
});
test('resources preserve null cross-origin fields and ambiguous intent candidates without secrets',()=>{
  const f=fixture();f.click();f.click();f.resource();
  const r=f.api().read().events.find(e=>e.type==='resource');assert.equal(r.data.request_start_ms,null);
  assert.equal(r.data.http_status,201);assert.equal(r.data.candidate_intents.length,2);
  assert.doesNotMatch(JSON.stringify(r),/SEEDED|token/);f.api().stop();
});

test('recorder badge distinguishes capture health from whether the form UI was found',()=>{
  const f=fixture({formPresent:false});
  const events=f.api().read().events;
  const beat=events.filter(e=>e.type==='heartbeat').at(-1);
  const form=events.find(e=>e.type==='ui.form');
  assert.equal(beat.data.form_detected,false);
  assert.equal(form.data.available,false);
  f.api().stop();
});

test('stop removes handlers and observers; fresh document gets explicit missing-prefix boundary',()=>{
  const f=fixture();f.api().stop();assert.equal(f.api().active,false);assert.equal(f.listeners.size,0);
  assert.equal(f.observers[0].disconnected,true);assert.equal(f.intervals.size,0);
  const fresh=fixture();assert.equal(fresh.api().read().events[0].data.missing_prefix,true);fresh.api().stop();
});
test('collector allowlist strips adversarial nested content from page-origin records',()=>{
  const r=sanitizePageEvent({source_sequence:1,document_id:'page-100-1',page_ms:1,type:'ui.intent',
    data:{intent_id:'intent-1',quantity_shares:7,price_rial:100,Authorization:'SEEDED_AUTH',nested:{cookie:'SEEDED_COOKIE'}}});
  assert.equal(r.data.quantity_shares,7);assert.doesNotMatch(JSON.stringify(r),/SEEDED|Authorization|nested/);
  assert.equal(sanitizePageEvent({source_sequence:1,document_id:'page-100-1',type:'secret',data:{secret:'SEEDED'}}),null);
});
