import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import crypto from 'node:crypto';
import {serve} from '../scripts/serve_board_inbrowser.mjs';

const request=(url,options={})=>new Promise((resolve,reject)=>{
  const req=http.request(url,{...options,agent:false},res=>{
    const chunks=[];res.on('data',chunk=>chunks.push(chunk));
    res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks).toString('utf8')}));
  });
  req.on('error',reject);req.end();
});

test('temporary loader serves only public monitor code on its exact local route',async()=>{
  const {server,url,bundle_sha256}=await serve({ttlMs:30000});
  try{
    const good=await request(url);
    assert.equal(good.status,200);
    assert.match(good.headers['content-security-policy'],/default-src 'none'/);
    assert.equal(good.headers['cache-control'],'no-store');
    const escaped=good.body.match(/<pre id="bundle" hidden>([\s\S]*?)<\/pre>/)[1];
    const bundle=JSON.parse(escaped.replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&'));
    assert.equal(bundle.bundle_sha256,bundle_sha256);
    assert.equal(crypto.createHash('sha256').update(bundle.expression).digest('hex'),bundle_sha256);
    assert.equal((await request(new URL('/wrong',url))).status,404);
    assert.equal((await request(url,{method:'POST'})).status,404);
    assert.equal((await request(url,{headers:{host:'example.invalid'}})).status,404);
  }finally{await new Promise(resolve=>server.close(resolve));}
});
