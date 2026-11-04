/** Temporary loopback-only transport for the project's public monitor code. */
import http from 'node:http';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import path from 'node:path';
import {build} from './build_board_inbrowser.mjs';

const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
export async function serve({ttlMs=300000}={}){
  if(!Number.isSafeInteger(ttlMs)||ttlMs<1000||ttlMs>3600000)throw new Error('عمر بارگذار نامعتبر است');
  const bundle=await build(),route='/'+crypto.randomBytes(20).toString('hex');
  const body='<!doctype html><meta charset="utf-8"><title>BRS monitor loader</title>'+
    '<p>بارگذار موقت کد عمومی پایش تابلو</p><pre id="bundle" hidden>'+escape(JSON.stringify(bundle))+'</pre>';
  const server=http.createServer((req,res)=>{
    if(req.headers.host!==`127.0.0.1:${server.address().port}`||req.method!=='GET'||req.url!==route){
      res.writeHead(404);res.end();return;
    }
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store',
      'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"});
    res.end(body);
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const timer=setTimeout(()=>server.close(),ttlMs);timer.unref();
  server.on('close',()=>clearTimeout(timer));
  return {server,url:`http://127.0.0.1:${server.address().port}${route}`,bundle_sha256:bundle.bundle_sha256};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  const {url,bundle_sha256}=await serve();console.log(JSON.stringify({url,bundle_sha256}));
}
