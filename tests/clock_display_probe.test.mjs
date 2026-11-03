import test from 'node:test';
import assert from 'node:assert/strict';
import {parseClockSeconds,clockLabelEpoch,analyzeDisplayNode} from '../data_collector/clock_display_probe.mjs';

test('Persian, Arabic, NBSP, 12-hour and invalid clock text',()=>{
  assert.equal(parseClockSeconds('۲۳ : ۱۵ : ۰۹'),83709);
  assert.equal(parseClockSeconds('٢٣\u00a0:\u00a0١٥\u00a0:\u00a0٠٩'),83709);
  assert.equal(parseClockSeconds('11 : 15 : 09PM'),83709);
  assert.equal(parseClockSeconds('12:00:00AM'),0);
  assert.equal(parseClockSeconds('00:00:00AM'),0);
  assert.equal(parseClockSeconds('12:00:00PM'),43200);
  for(const bad of ['24:00:00','23:60:00','23:59:60','13:00:00PM','12:00','',null,'date 23:15:09'])assert.equal(parseClockSeconds(bad),null);
});
test('Tehran date boundary uses the nearest displayed day',()=>{
  const midnight=Date.parse('2026-10-25T00:00:00+03:30');
  assert.equal(clockLabelEpoch('23:59:59',midnight+50),midnight-1000);
  assert.equal(clockLabelEpoch('00:00:00',midnight-50),midnight);
  assert.equal(clockLabelEpoch('08:45:00',Date.parse('2026-10-25T08:45:00.250+03:30')),Date.parse('2026-10-25T08:45:00+03:30'));
  assert.equal(clockLabelEpoch('not a time',midnight),null);
});
test('An initial sample is not a boundary; transient minute renders are excluded',()=>{
  const base=Date.parse('2026-10-25T08:44:59+03:30');
  const node={index:0,rows:[{text:'08:44:59',window_after_ms:base+500,previous_read_ms:null},{text:'08:44:00',window_after_ms:base+1100,previous_read_ms:base+1080},{text:'08:45:00',window_after_ms:base+1101,previous_read_ms:base+1100},{text:'08:45:01',window_after_ms:base+2100,previous_read_ms:base+2080}]};
  const report=analyzeDisplayNode(node);
  assert.equal(report.transient_labels_excluded,1);assert.equal(report.normal_second_transitions,2);assert.equal(report.appearance_lag_median_ms,100.5);assert.equal(report.discontinuities.length,0);
});
test('A clock jump stays a discontinuity and is not passed off as jitter',()=>{
  const base=Date.parse('2026-10-25T08:45:00+03:30');
  const rows=['08:45:00','08:45:01','08:45:04','08:45:05'].map((text,i)=>({text,window_after_ms:base+500+i*1000,previous_read_ms:i?base+480+i*1000:null}));
  const result=analyzeDisplayNode({index:0,rows});assert.equal(result.discontinuities.length,1);assert.equal(result.discontinuities[0].label_delta_ms,3000);assert.equal(result.normal_second_transitions,2);
});
test('Empty or invalid evidence remains unknown',()=>{
  const result=analyzeDisplayNode({index:0,rows:[{text:'--:--:--',window_after_ms:0}]});assert.equal(result.appearance_lag_median_ms,null);assert.equal(result.normal_second_transitions,0);
});
