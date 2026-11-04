import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {compareClockTabs} from '../scripts/clock_compare_cua.mjs';

const outputRoot=path.resolve('tests/.timing-output');
async function workspace(){await fs.mkdir(outputRoot,{recursive:true});return fs.mkdtemp(path.join(outputRoot,'clock-coordinator-'));}
test('Coordinator isolates a failed clock and hashes every saved round',async()=>{
  const root=await workspace(),calls=[];
  const good={site:'fixture',selector:'#test-clock',cdp:{async send(method,args){calls.push({method,args});const now=Date.now();return {result:{value:{start_windows_ms:now,end_windows_ms:now,max_poll_gap_ms:0,visibility_counts:{visible:1},nodes:[{index:0,rows:[]}]}}};}}};
  const bad={site:'failed',selector:'#test-clock',cdp:{async send(){throw new Error('test fixture unavailable');}}};
  const {folder,report}=await compareClockTabs([good,bad],{workspace:root,rounds:2,durationMs:1000});
  assert.equal(report.complete,false);assert.equal(report.errors.length,2);assert.equal(report.summaries.length,2);
  assert.equal(report.summaries[0].nodes[0].appearance_lag_median_ms,null);
  assert.ok(calls.every(c=>c.method==='Runtime.evaluate' && c.args.awaitPromise && c.args.returnByValue));
  assert.equal(report.order_actions,0);assert.equal(report.exchange_clock_verified,false);
  const manifest=JSON.parse(await fs.readFile(path.join(folder,'manifest.json'),'utf8'));
  assert.equal(manifest.files.length,3);
  for(const entry of manifest.files)assert.equal(crypto.createHash('sha256').update(await fs.readFile(path.join(folder,entry.name))).digest('hex'),entry.sha256);
});
test('Page and host time mismatch is recorded as a failed measurement',async()=>{
  const root=await workspace();
  const spec={site:'fixture',selector:'#clock',cdp:{async send(){return {result:{value:{start_windows_ms:1,end_windows_ms:2,nodes:[]}}};}}};
  const {report}=await compareClockTabs([spec],{workspace:root,rounds:1,durationMs:1000});
  assert.equal(report.complete,false);assert.equal(report.summaries.length,0);assert.match(report.errors[0].error,/bracket mismatch/);
});
test('An output junction is rejected before any clock is queried',async()=>{
  const root=await workspace(),outside=await workspace();await fs.mkdir(path.join(root,'data'),{recursive:true});
  await fs.symlink(outside,path.join(root,'data/private'),process.platform==='win32'?'junction':'dir');
  let called=false;
  await assert.rejects(compareClockTabs([{site:'fixture',selector:'#clock',cdp:{async send(){called=true;}}}],{workspace:root,rounds:1,durationMs:1000}),/Reparse output path rejected/);
  assert.equal(called,false);assert.deepEqual(await fs.readdir(outside),[]);
});
