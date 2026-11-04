/** SYNTHETIC example only. Never imports a browser or contacts a service. */
import path from 'node:path';
import {createJournal,writeReport} from '../scripts/farabi_timing.mjs';

const fixtureRoot=path.resolve('tests/.timing-output');
const directory=path.join(fixtureRoot,'example-'+new Date().toISOString().replace(/[:.]/g,'-'));
const journal=await createJournal(directory,{synthetic:true,fixtureRoot});
await journal.append([
  {type:'ui.intent',document_id:'page-1000000-1',clock_segment:1,page_ms:100,
    data:{intent_id:'intent-1',trusted:false,quantity_shares:7,price_rial:100},synthetic:true},
  {type:'resource',document_id:'page-1000000-1',clock_segment:1,page_ms:320,
    data:{candidate_intents:['intent-1'],host:'127.0.0.1',path_class:'orders',start_ms:120,
      duration_ms:180,response_end_ms:300,http_status:201,method:null},synthetic:true},
  {type:'ui.orders',document_id:'page-1000000-1',clock_segment:1,page_ms:500,
    data:{rows:[{broker_state:'failed',quantity_shares:7,filled_shares:0,price_rial:100}],
      exchange_acceptance_time:null,queue_rank:null},synthetic:true},
]);
await journal.close();
await writeReport(directory,{synthetic:true,health:'SAVED',gaps:[],note:'SYNTHETIC: HTTP 201 with business rejection; no real service contacted'});
console.log(JSON.stringify({synthetic:true,directory}));
