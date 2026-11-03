import test from 'node:test';
import assert from 'node:assert/strict';
import {BoardPressureAlerts,nearLimitDepth,validatePressureRule} from '../core/board_pressure_alerts.mjs';

const make=(extra={})=>({key:'TEST00000001',symbol:'آزمون',market_day:'2026-10-20',
  observed_at:'2026-10-20T10:00:00.000+03:30',last_price:100,low_limit:100,high_limit:110,
  volume:1000000,sell_queue:20000000,buy_queue:0,trade_age_seconds:1,
  rows:[{bid:{price:100,volume:1000,volume_precision:'integer',within_band:true},
    ask:{price:100,volume:20000000,volume_precision:'integer',within_band:true}},
    ...Array.from({length:4},()=>({bid:null,ask:null}))],...extra});
const engine=()=>new BoardPressureAlerts({max_gap_seconds:3,min_queue:1000000,min_drop:100000,drop_fraction:0.3,min_traded:100000});

test('declining sell queue warns before the final one-million threshold, without a buy signal',()=>{
  const e=engine();e.observe(make(),0);
  const rows=make().rows;rows[0].bid.volume=301000;rows[0].ask.volume=14000000;
  const result=e.observe(make({sell_queue:14000000,volume:1300000,rows}),1000);
  assert.deepEqual(result.events.map(x=>x.kind),['sell_queue_weakening','sell_queue_weakening_with_trades']);
  assert.equal(result.events[0].queue_now,14000000);
  assert.equal(result.events[0].traded_volume,300000);
  assert.equal(result.events[0].near_opposing_depth_change,300000);
  assert.equal(result.events[0].opposing_depth_increased,true);
  assert.equal(result.events[0].accepted_for_signals,false);
  assert.match(result.events[0].attribution,/not_separable/);
});

test('buy queue erosion and increased supply near ceiling are kept distinct from causation',()=>{
  const e=engine();const initial=make({sell_queue:0,buy_queue:20000000});
  initial.rows[0]={bid:{price:110,volume:20000000,volume_precision:'integer',within_band:true},
    ask:{price:110,volume:50000,volume_precision:'integer',within_band:true}};
  e.observe(initial,0);
  const next=make({sell_queue:0,buy_queue:10000000,volume:1300000});
  next.rows[0]={bid:{price:110,volume:10000000,volume_precision:'integer',within_band:true},
    ask:{price:110,volume:250000,volume_precision:'integer',within_band:true}};
  const events=e.observe(next,1000).events;
  assert.deepEqual(events.map(x=>x.kind),['buy_queue_weakening','buy_queue_weakening_with_trades']);
  assert.equal(events[0].near_opposing_depth_change,200000);
  assert.equal(events[0].opposing_depth_increased,true);
});

test('stale last trade does not become an accompanying trading-volume claim',()=>{
  const e=engine();e.observe(make(),0);
  const events=e.observe(make({sell_queue:10000000,volume:2000000,trade_age_seconds:90}),1000).events;
  assert.deepEqual(events.map(x=>x.kind),['sell_queue_weakening']);
  assert.equal(events[0].trade_counter_fresh,false);
});

test('book side disappearing remains unknown and cannot bridge a later baseline',()=>{
  const e=engine();e.observe(make(),0);
  const empty=make({sell_queue:null});for(const row of empty.rows)row.ask=null;
  const event=e.observe(empty,1000).events[0];
  assert.equal(event.kind,'sell_side_empty');assert.equal(event.queue_now,null);assert.equal(event.break_proven,false);
  assert(!e.observe(make({sell_queue:1,volume:2000000}),2000).events.some(x=>x.kind.startsWith('sell_')));
});

test('empty whole book and rounded queue do not masquerade as a broken queue',()=>{
  const e=engine();e.observe(make(),0);
  const empty=make({sell_queue:null,rows:Array.from({length:5},()=>({bid:null,ask:null}))});
  assert.deepEqual(e.observe(empty,1000).events,[]);
  const f=engine();f.observe(make(),0);
  const rounded=make({sell_queue:null});rounded.rows[0].ask.volume=null;rounded.rows[0].ask.volume_precision='rounded_unknown';
  assert.deepEqual(f.observe(rounded,1000).events,[]);
});

test('best quote moving away from limit emits a distinct observable event',()=>{
  const e=engine();e.observe(make(),0);
  const next=make({sell_queue:0,volume:2000000});next.rows[0].ask.price=101;next.rows[0].ask.volume=1000;
  const events=e.observe(next,1000).events;
  assert(events.some(x=>x.kind==='sell_quote_left_limit'&&x.top_price_rial===101));
});

test('turnover acceleration compares two non-overlapping continuously sampled windows',()=>{
  const e=new BoardPressureAlerts({min_traded:1000,burst_seconds:10,baseline_seconds:20,max_gap_seconds:3,acceleration_factor:2});
  let vol=1000000,events=[];
  for(let i=0;i<=31;i++){
    if(i>0)vol+=i<=21?100:1000;
    events.push(...e.observe(make({volume:vol}),i*1000).events);
  }
  const a=events.find(x=>x.kind==='trade_volume_acceleration');
  assert(a);assert(a.rate_ratio>=2);assert.equal(a.recent_seconds,10);assert.equal(a.prior_seconds,20);
  assert.match(a.interpretation,/does_not_identify_aggressor/);
});

test('zero baseline turnover is unknown rather than an infinite acceleration',()=>{
  const e=new BoardPressureAlerts({min_traded:1000,burst_seconds:10,baseline_seconds:20,max_gap_seconds:3});
  let events=[];for(let i=0;i<=31;i++)events.push(...e.observe(make({volume:1000000+(i>21?(i-21)*1000:0)}),i*1000).events);
  assert(!events.some(x=>x.kind==='trade_volume_acceleration'));
});

test('absolute turnover burst can be observed without claiming it is buying pressure',()=>{
  const e=engine();e.observe(make(),0);
  const burst=e.observe(make({volume:2100000}),1000).events;
  assert.deepEqual(burst.map(x=>x.kind),['trade_volume_burst']);
  assert.equal(burst[0].traded_volume,1100000);
  assert.match(burst[0].interpretation,/does_not_identify_aggressor/);
  assert.equal(e.observe(make({volume:2200000}),2000).events.length,0);
});

test('stale trade timestamp cannot trigger an absolute turnover burst',()=>{
  const e=engine();e.observe(make(),0);
  assert.deepEqual(e.observe(make({volume:3000000,trade_age_seconds:100}),1000).events,[]);
});

test('fresh volume after a stale interval starts a new baseline before volume alerts',()=>{
  const e=engine();e.observe(make(),0);
  e.observe(make({sell_queue:15000000,volume:1200000,trade_age_seconds:90}),1000);
  const first=e.observe(make({sell_queue:14000000,volume:2500000,trade_age_seconds:1}),2000).events;
  assert.deepEqual(first.map(x=>x.kind),['sell_queue_weakening']);
  assert.equal(first[0].traded_volume,null);assert.equal(first[0].trade_delta_usable,false);
  const next=e.observe(make({sell_queue:13000000,volume:2700000,trade_age_seconds:1}),3000).events;
  assert.deepEqual(next.map(x=>x.kind),['sell_queue_weakening_with_trades']);
  assert.equal(next[0].traded_volume,200000);assert.equal(next[0].traded_interval_seconds,1);
});

test('gap, new session, band, identity or counter change reset observations',()=>{
  for(const [q,ms,why] of [[make({sell_queue:1}),4000,'sampling_gap'],[make({market_day:'2026-10-21'}),1000,'new_session'],
    [make({volume:1}),1000,'counter_decreased'],[make({high_limit:111}),1000,'price_band_changed'],
    [make({symbol:'دیگر'}),1000,'identity_changed']]){
    const e=engine();e.observe(make(),0);const result=e.observe(q,ms);
    assert.equal(result.baseline_reason,why);assert.deepEqual(result.events,[]);
  }
});

test('rounded near-limit volume remains unknown and does not imply zero opposition',()=>{
  const q=make();q.rows[0].bid.volume=null;q.rows[0].bid.volume_precision='rounded_unknown';
  assert.equal(nearLimitDepth(q,'bid',0.02),null);
});

test('an entirely absent side cannot prove an increase in opposing depth',()=>{
  const e=engine(),initial=make({sell_queue:0,buy_queue:20000000});
  initial.rows[0]={bid:{price:110,volume:20000000,volume_precision:'integer',within_band:true},ask:null};
  assert.equal(nearLimitDepth(initial,'ask',0.02),null);
  e.observe(initial,0);
  const next=make({sell_queue:0,buy_queue:10000000});
  next.rows[0]={bid:{price:110,volume:10000000,volume_precision:'integer',within_band:true},
    ask:{price:110,volume:250000,volume_precision:'integer',within_band:true}};
  const event=e.observe(next,1000).events[0];
  assert.equal(event.near_opposing_depth_before,null);
  assert.equal(event.near_opposing_depth_change,null);
  assert.equal(event.opposing_depth_increased,null);
});

test('an observed in-band level outside the near band has zero visible near depth',()=>{
  const q=make();q.rows[0].bid.price=105;
  assert.equal(nearLimitDepth(q,'bid',0.02),0);
  q.rows[0].bid.price=120;
  assert.equal(nearLimitDepth(q,'bid',0.02),null);
});

test('rule input and event cooldown are validated',()=>{
  for(const bad of [{min_queue:0},{min_drop:true},{drop_fraction:NaN},{acceleration_factor:1},
    {sides:['buy','buy']},{depth_fraction:0.5},{unused:1}])assert.throws(()=>validatePressureRule(bad));
  const e=engine();e.observe(make(),0);const first=e.observe(make({sell_queue:10000000}),1000);
  assert.equal(first.events.length,1);assert.equal(e.observe(make({sell_queue:9000000}),2000).events.length,0);
  assert.throws(()=>e.observe(make(),2000),/زمان/);
});

test('a second qualifying decline suppressed by cooldown can alert when cooldown ends',()=>{
  const e=new BoardPressureAlerts({cooldown_seconds:5});
  e.observe(make(),0);
  const first=e.observe(make({sell_queue:10000000,volume:1300000}),1000);
  assert(first.events.some(x=>x.kind==='sell_queue_weakening'));
  assert(first.events.some(x=>x.kind==='sell_queue_weakening_with_trades'));
  e.observe(make({sell_queue:20000000,volume:1300000}),2000);
  const suppressed=e.observe(make({sell_queue:10000000,volume:1600000}),3000);
  assert(!suppressed.events.some(x=>x.kind.startsWith('sell_queue_weakening')));
  const stillCooling=e.observe(make({sell_queue:10000000,volume:1600000}),4000);
  assert(!stillCooling.events.some(x=>x.kind.startsWith('sell_queue_weakening')));
  const delayed=e.observe(make({sell_queue:10000000,volume:1600000}),6000);
  assert(delayed.events.some(x=>x.kind==='sell_queue_weakening'));
  assert(delayed.events.some(x=>x.kind==='sell_queue_weakening_with_trades'));
  assert(!e.observe(make({sell_queue:10000000,volume:1600000}),7000).events.some(x=>x.kind.startsWith('sell_queue_weakening')));
});

test('a burst suppressed by cooldown remains eligible while it continues',()=>{
  const e=new BoardPressureAlerts({burst_seconds:10,cooldown_seconds:30,burst_volume:1000000});
  let volume=1000000;const bursts=[];
  for(let second=0;second<=32;second++){
    if([1,13,21,29].includes(second))volume+=1100000;
    const events=e.observe(make({volume}),second*1000).events;
    for(const event of events)if(event.kind==='trade_volume_burst')bursts.push(second);
  }
  assert.deepEqual(bursts,[1,31]);
});
