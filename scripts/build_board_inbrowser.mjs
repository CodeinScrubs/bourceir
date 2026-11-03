/** Build a self-contained IAB-only monitor from audited source files. */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const inputs=['data_collector/farabi_board.mjs','core/board_pressure_alerts.mjs','dashboard/board_inbrowser_bootstrap.js'];
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
export async function build(){
  const sources=await Promise.all(inputs.map(async name=>({name,body:await fs.readFile(path.join(root,name),'utf8')})));
  const sourceHashes=Object.fromEntries(sources.map(({name,body})=>[name,hash(body)]));
  for(const source of sources.slice(0,2)){
    if(/^\s*import\s/m.test(source.body))throw new Error('ورودی مرورگر نباید import وابسته داشته باشد');
    source.body=source.body.replace(/^export\s+/gm,'');
    if(/^\s*export\s/m.test(source.body))throw new Error('تبدیل ورودی مرورگر ناموفق بود');
  }
  const expression='(()=>{\n'+sources[0].body+'\n'+sources[1].body+'\nreturn (\n'+sources[2].body+'\n);\n})()';
  for(const name of inputs)if(hash(await fs.readFile(path.join(root,name)))!==sourceHashes[name])
    throw new Error('کد هنگام ساخت بسته تغییر کرد');
  return {version:'board-inbrowser-bundle-v1',source_sha256:sourceHashes,bundle_sha256:hash(expression),expression};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  const bundle=await build(),target=path.resolve(process.argv[2]||path.join(root,'data/private/farabi-board/inbrowser-bundle.json'));
  if(!target.startsWith(path.join(root,'data/private')+path.sep))throw new Error('بسته باید در data/private باشد');
  await fs.mkdir(path.dirname(target),{recursive:true});await fs.writeFile(target,JSON.stringify(bundle,null,2),{flag:'wx'});
  console.log(JSON.stringify({file:target,bundle_sha256:bundle.bundle_sha256,source_sha256:bundle.source_sha256}));
}
