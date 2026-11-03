/** Load only in the approved Browser/CUA REPL with existing, inspected tab handles. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {observeClockDisplay, analyzeDisplayNode} from '../data_collector/clock_display_probe.mjs';

function tehranIso(date=new Date()) {
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Tehran',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'longOffset'}).formatToParts(date).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${parts.timeZoneName.replace('GMT','')}`;
}
async function rejectRedirects(root, target) {
  const relative=path.relative(root,target);
  if(relative.startsWith('..') || path.isAbsolute(relative))throw new Error('Output escapes workspace');
  for(let current=target;;current=path.dirname(current)) {
    try {if((await fs.lstat(current)).isSymbolicLink())throw new Error('Reparse output path rejected');}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(current===root)break;
  }
}

export async function compareClockTabs(specifications,{workspace,rounds=3,durationMs=8000}={}) {
  if(!path.isAbsolute(workspace??'') || !Number.isInteger(rounds) || rounds<1 || rounds>5 || !Number.isFinite(durationMs) || durationMs<1000 || durationMs>10000 || !Array.isArray(specifications) || specifications.length<1 || specifications.length>8)throw new Error('Invalid clock comparison configuration');
  const root=path.resolve(workspace),parent=path.join(root,'data/private/clock-comparison');
  await rejectRedirects(root,parent);await fs.mkdir(parent,{recursive:true});
  const folder=path.join(parent,`session-${new Date().toISOString().replace(/[:.]/g,'-')}-${crypto.randomBytes(3).toString('hex')}`);
  await rejectRedirects(root,folder);await fs.mkdir(folder);
  const summaries=[],errors=[],bindings=[];
  const self=fileURLToPath(import.meta.url),probe=fileURLToPath(new URL('../data_collector/clock_display_probe.mjs',import.meta.url));
  for(const filename of [self,probe])bindings.push({name:path.basename(filename),sha256:crypto.createHash('sha256').update(await fs.readFile(filename)).digest('hex')});
  for(let round=1;round<=rounds;round++) {
    const results=await Promise.allSettled(specifications.map(async spec=>{
      if(!/^[a-z_]+$/.test(spec.site??'') || typeof spec.selector!=='string' || typeof spec.cdp?.send!=='function')throw new Error('Invalid observed tab specification');
      const hostBefore=Date.now();
      const result=await spec.cdp.send('Runtime.evaluate',{expression:`(${observeClockDisplay.toString()})(${JSON.stringify({selector:spec.selector,durationMs,intervalMs:20})})`,returnByValue:true,awaitPromise:true},{timeoutMs:durationMs+5000});
      const hostAfter=Date.now(),data=result.result?.value;
      if(!data || data.error || result.exceptionDetails)throw new Error(data?.error??'Clock observation failed');
      if(data.start_windows_ms<hostBefore-100 || data.end_windows_ms>hostAfter+100)throw new Error('Page/host wall time bracket mismatch');
      return {site:spec.site,tab_id:spec.tabId??null,url:spec.url??null,fresh_tab:spec.fresh??null,host_before_ms:hostBefore,host_after_ms:hostAfter,result:data};
    }));
    const records=results.map((result,index)=>result.status==='fulfilled'?result.value:{site:specifications[index].site,error:String(result.reason)});
    for(const record of records) {
      if(record.error){errors.push({round,site:record.site,error:record.error});continue;}
      summaries.push({round,site:record.site,tab_id:record.tab_id,url:record.url,fresh_tab:record.fresh_tab,start_windows_ms:record.result.start_windows_ms,end_windows_ms:record.result.end_windows_ms,max_poll_gap_ms:record.result.max_poll_gap_ms,visibility_counts:record.result.visibility_counts,nodes:record.result.nodes.map(analyzeDisplayNode)});
    }
    await fs.writeFile(path.join(folder,`round-${round}.json`),JSON.stringify({recorded_at_tehran:tehranIso(),round,records},null,2));
  }
  const report={schema_version:1,recorded_at_tehran:tehranIso(),complete:errors.length===0,rounds,duration_ms_per_round:durationMs,probe_sources:bindings,summaries,errors,windows_settings_changed:false,order_actions:0,server_clock_accuracy_verified:false,exchange_clock_verified:false,measurement:'new displayed second appearance lag relative to Windows; DOM observation and UI scheduling are included; raw samples are preserved'};
  await fs.writeFile(path.join(folder,'report.json'),JSON.stringify(report,null,2));
  const manifest=[];
  for(const name of await fs.readdir(folder))manifest.push({name,sha256:crypto.createHash('sha256').update(await fs.readFile(path.join(folder,name))).digest('hex')});
  await fs.writeFile(path.join(folder,'manifest.json'),JSON.stringify({recorded_at_tehran:tehranIso(),files:manifest},null,2));
  return {folder,report};
}
