/** بازاجرای آفلاین ارزیابی‌های ثبت‌شده؛ کد ناشناسِ آرشیو اجرا نمی‌شود. */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';
import {BoardAlerts as LegacyBoardAlerts,version as legacyVersion} from '../core/board_alerts.mjs';
import {BoardAlerts as PreviousBoardAlerts,version as previousVersion} from '../core/board_alerts_v2.mjs';
import {BoardAlerts,version} from '../core/board_alerts_v3.mjs';
import {parseBoard} from '../data_collector/farabi_board.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const sharedFiles=['data_collector/farabi_board.mjs','scripts/farabi_watch.mjs','dashboard/board_watch.html'];
// Exact known v1 producer, including its decoder. Archive source is never executed.
const legacyCode=Object.freeze({
  'core/board_alerts.mjs':'c3bbbf1ffd303a17ae539aebc42bc96d9b5633a3e5717ace4a756a3c2d011ebb',
  'data_collector/farabi_board.mjs':'4741f3c2e7db3756014c6bd5bd1a4951c0b9a3108010f56b8a90982234ad6b46',
  'scripts/farabi_watch.mjs':'36f55f071b8243b320ade8fbf63c43dd2519b52c565cf8822b20c8ecd835d25c',
  'dashboard/board_watch.html':'1d4cab2bdc8b806166db057095cfd35deff38e3b45ef55cf5b2c945537b0ce94',
});
// Exact v2 producer and decoder; the archived copies are compared, never executed.
const previousCode=Object.freeze({
  'core/board_alerts_v2.mjs':'b378456c8a84e39c0bb2a7ddcf1416a612cd698d3954f1747f059e389c6d55f7',
  'data_collector/farabi_board.mjs':'4741f3c2e7db3756014c6bd5bd1a4951c0b9a3108010f56b8a90982234ad6b46',
  'scripts/farabi_watch.mjs':'65776289b3229d0ff16f08aab4fa0aa28dc5fa172a3a4382b51eb91553032de7',
  'dashboard/board_watch.html':'1d4cab2bdc8b806166db057095cfd35deff38e3b45ef55cf5b2c945537b0ce94',
});
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
export async function reviewArchive(directory) {
  const base=path.resolve(directory),plan=JSON.parse(await fsp.readFile(path.join(base,'plan.json'),'utf8'));
  const legacy=plan.version===legacyVersion,previous=plan.version===previousVersion;
  const engineFile=legacy?'core/board_alerts.mjs':previous?'core/board_alerts_v2.mjs':'core/board_alerts_v3.mjs';
  const knownCode=legacy?legacyCode:previous?previousCode:null;
  const codeFiles=[engineFile,...sharedFiles];
  if((!legacy&&!previous&&plan.version!==version)||!plan.code||!isDeepStrictEqual(Object.keys(plan.code).sort(),[...codeFiles].sort()))throw new Error('قرارداد یا فهرست کد شناخته‌شده نیست');
  for(const file of codeFiles){const archived=await fsp.readFile(path.join(base,'code',file));
    const expected=knownCode?knownCode[file]:hash(await fsp.readFile(path.join(root,file)));
    const decoder=knownCode&&[engineFile,'data_collector/farabi_board.mjs'].includes(file);
    if(hash(archived)!==plan.code[file]||expected!==plan.code[file]||
       (decoder&&hash(await fsp.readFile(path.join(root,file)))!==expected))
      throw new Error('کد محفوظ با نسخه بازبین برابر نیست؛ کد آرشیو اجرا نشد');}
  let seq=0,previousHash='0'.repeat(64),complete=false,started=false,observations=0,evaluations=0,alerts=0;
  const Engine=legacy?LegacyBoardAlerts:previous?PreviousBoardAlerts:BoardAlerts;
  const sources=new Map(),engine=new Engine(plan.rule),expected=[];
  for(const [key,rule]of Object.entries(plan.rule_overrides||{}))engine.configureForKey(key,rule);
  const lines=readline.createInterface({input:fs.createReadStream(path.join(base,'events.jsonl')),crlfDelay:Infinity});
  for await(const line of lines){
    if(line.length>1000000)throw new Error('رکورد بیش از حد بزرگ است');
    const parsed=JSON.parse(line),{sha256,...record}=parsed;
    if(complete||record.seq!==++seq||record.previous_sha256!==previousHash||hash(JSON.stringify(record))!==sha256)
      throw new Error('زنجیره، ترتیب یا پایان دفتر ناسازگار است');
    previousHash=sha256;const p=record.payload;
    if(record.type==='started'){if(started||seq!==1||!isDeepStrictEqual(p,plan))throw new Error('برنامه شروع با دفتر برابر نیست');started=true;}
    else if(!started)throw new Error('دفتر بدون شروع است');
    else if(record.type==='rule_changed'){if(expected.length)throw new Error('هشدار ثبت نشده است');if(p.key)engine.configureForKey(p.key,p.rule);else engine.configure(p.rule);}
    else if(record.type==='baseline_invalidated')engine.invalidate(p.key);
    else if(record.type==='dom_observation'){sources.set(seq,p);observations++;}
    else if(record.type==='evaluation'){
      if(expected.length)throw new Error('هشدار ثبت نشده است');
      const source=sources.get(p.observation_seq);if(!source||source.monotonic_ms!==p.monotonic_ms)throw new Error('ارزیابی به نمونه پیوند ندارد');
      sources.delete(p.observation_seq);const q=parseBoard(source.raw,source.received_at);
      const weekday=new Date(q.market_day+'T12:00:00+03:30').getUTCDay(),time=q.observed_at.slice(11,19);
      const open=[6,0,1,2,3].includes(weekday)&&time>='09:00:00'&&time<'12:30:00';
      if(p.key!==q.key||p.session_open!==open)throw new Error('هویت یا وضعیت جلسه بازحساب نمی‌شود');
      let result={events:[],reset:true,baseline_reason:'outside_regular_session'};
      if(open)result=engine.observe(q,p.monotonic_ms);else engine.invalidate(q.key);
      if(!isDeepStrictEqual(result,p.result))throw new Error('نتیجه هشدار بازحساب نمی‌شود');
      expected.push(...result.events);evaluations++;
    }else if(record.type==='alert'){if(!expected.length||!isDeepStrictEqual(expected.shift(),p))throw new Error('هشدار با ارزیابی برابر نیست');alerts++;}
    else if(record.type==='stopped')complete=true;
    else if(!['read_error','parse_rejected'].includes(record.type))throw new Error('نوع رکورد ناشناخته است');
    // فقط پیوندهای اخیر لازم‌اند؛ نبود ارزیابی برای ردیف ردشده، دریافت معتبر نمی‌سازد.
    if(sources.size>1000)sources.delete(sources.keys().next().value);
  }
  if(expected.length||!started)throw new Error('دفتر بدون شروع یا دارای هشدار ثبت‌نشده است');
  return {version:plan.version,records:seq,observations,evaluations,alerts,hash_chain_valid:true,evaluations_reproduced:true,
    complete,independent_source_or_completeness_verified:false,accepted_for_signals:false};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  reviewArchive(process.argv[2]).then(r=>console.log(JSON.stringify(r,null,2))).catch(e=>{console.error(e.message);process.exitCode=1;});
