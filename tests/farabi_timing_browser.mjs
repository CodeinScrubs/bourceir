/** Invoke only through approved CUA/CDP on our synthetic loopback fixture. */
export function localClickBenchmark(){
  if(location.hostname!=='127.0.0.1'||location.pathname!=='/tests/farabi-timing')
    throw new Error('Synthetic benchmark scope required');
  window.syntheticBenchmark=true;
  const button=document.querySelector('form button');
  const samples=[];
  for(let i=0;i<1000;i++){
    const start=performance.now();
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}));
    samples.push(performance.now()-start);
  }
  samples.sort((a,b)=>a-b);
  return {n:samples.length,median_ms:samples[500],p95_ms:samples[950],max_ms:samples.at(-1),
    sum_ms:samples.reduce((a,b)=>a+b,0),
    scope:'synchronous local event dispatch only; excludes timer paint, broker and network load',
    clock_resolution_note:'performance.now quantization may produce zeros'};
}
