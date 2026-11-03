/* Bundled with the read-only Farabi DOM parser and descriptive alert engine. */
(() => {
  'use strict';
  const slot='__BRS_VISIBLE_PRESSURE_MONITOR_V5__';
  if(window[slot])return {status:'already_installed',version:pressureVersion};
  if(document.querySelector('[data-brs-board-monitor]'))throw new Error('یک پایش تابلو باز است؛ ابتدا آن را ببندید.');
  if(!isBoardUrl(location.href))throw new Error('ابتدا تابلوی یک نماد در فارابی را باز کنید');
  const initialKey=new URL(location.href).searchParams.get('isin');
  const host=document.createElement('div');
  host.setAttribute('data-brs-board-monitor','visible-pressure-v5');
  const shadow=host.attachShadow({mode:'open'});
  shadow.innerHTML=`<style>
    :host{all:initial} .box{font:13px/1.65 Tahoma,sans-serif;direction:rtl;text-align:right;
      position:fixed;bottom:52px;left:12px;z-index:2147483646;width:min(330px,calc(100vw - 28px));
      max-height:calc(100vh - 65px);overflow:auto;background:#121e2c;color:#edf4ff;
      border:1px solid #80a4c5;border-radius:10px;box-shadow:0 6px 22px #0009;padding:10px;box-sizing:border-box}
    h2{font-size:15px;margin:0 0 4px}p{margin:5px 0}button,input,select{font:inherit;border:1px solid #768ba1;
      border-radius:5px;color:#eef5ff;background:#1c344a;padding:3px 6px}button{cursor:pointer}input{width:92px;direction:ltr}
    .row{display:flex;justify-content:space-between;gap:6px;align-items:center}.bad{color:#ffb1a5}.good{color:#9de3bb}
    .muted{color:#c3d0df;font-size:11px}.event{border-right:3px solid #ffce6b;padding:3px 6px;margin:5px 0;background:#243548}
    .fields{display:grid;grid-template-columns:1fr auto;gap:4px;align-items:center}a{color:#bbdfff}
    details{border-top:1px solid #4a5d71;margin-top:7px}summary{cursor:pointer}
    .box.compact #body{display:none}.box.compact{width:min(260px,calc(100vw - 28px))}</style>
    <section class="box" aria-label="هشدار سریع تابلو">
      <div class="row"><h2>هشدار سریع تابلو <b id="symbol"></b></h2><span><button id="collapse" type="button">جمع‌کردن</button> <button id="close" type="button" aria-label="بستن هشدار">×</button></span></div>
      <p id="health" class="bad" role="status">در انتظار نخستین مشاهده معتبر…</p>
      <div id="body">
      <p id="quote"></p><p id="fresh" class="muted"></p>
      <div class="row"><button id="sound" type="button">فعال‌کردن صدا</button><button id="test" type="button" disabled>آزمون صدا</button></div>
      <p class="muted">ساعت مستقل دفتر و تأخیر بورس نامعلوم است. هشدار توصیه یا پیش‌بینی نیست؛ بارگذاری مجدد صفحه آن را متوقف می‌کند.</p>
      <details><summary>تنظیم هشدار این نماد</summary><div class="fields">
        <label for="sides">سمت صف</label><select id="sides"><option value="both">هر دو</option><option value="sell">فروش</option><option value="buy">خرید</option></select>
        <label for="min_queue">حداقل صف اولیه (سهم)</label><input id="min_queue" type="number" min="1" step="1">
        <label for="min_drop">حداقل کاهش (سهم)</label><input id="min_drop" type="number" min="1" step="1">
        <label for="drop_pct">حداقل کاهش (٪)</label><input id="drop_pct" type="number" min="0.1" max="100" step="0.1">
        <label for="window_seconds">پنجره کاهش (ثانیه)</label><input id="window_seconds" type="number" min="3" max="3600" step="1">
        <label for="min_traded">حداقل حجم همراه (سهم)</label><input id="min_traded" type="number" min="1" step="1">
        <label for="burst_volume">حجم سریع ۲۰ ثانیه (سهم)</label><input id="burst_volume" type="number" min="1" step="1">
      </div><p><button id="save" type="button">اعمال و شروع مبنای تازه</button></p><p id="settings" class="muted"></p></details>
      <p><strong>رویدادهای اخیر</strong></p><div id="events" aria-live="polite" class="muted">هنوز رویدادی ثبت نشده است.</div>
      </div>
    </section>`;
  document.documentElement.append(host);
  const $=id=>shadow.getElementById(id), fmt=n=>n===null||n===undefined?'نامعلوم':Number(n).toLocaleString('fa-IR');
  const titles={sell_queue_weakening:'کاهش صف فروش',sell_queue_weakening_with_trades:'کاهش صف فروش همراه حجم معاملات',
    buy_queue_weakening:'کاهش صف خرید',buy_queue_weakening_with_trades:'کاهش صف خرید همراه حجم معاملات',
    sell_side_empty:'سمت عرضه خالی دیده شد؛ نیازمند بررسی',buy_side_empty:'سمت تقاضا خالی دیده شد؛ نیازمند بررسی',
    sell_quote_left_limit:'بهترین عرضه بالاتر از کف دیده شد',buy_quote_left_limit:'بهترین تقاضا پایین‌تر از سقف دیده شد',
    trade_volume_acceleration:'سرعت حجم معاملات افزایش یافت',trade_volume_burst:'حجم معاملات در بازه کوتاه زیاد شد'};
  let engine=new BoardPressureAlerts(),rule=validatePressureRule(),firstSymbol=null,timer=null,audio=null,closed=false,lastTick=null;
  const history=[],state={version:pressureVersion,key:initialKey,symbol:null,status:'starting',samples:0,events:0,
    max_gap_ms:0,last_observed_at:null,last_trade_age_seconds:null,last_error:null,sound_enabled:false,
    independent_book_clock_verified:false,accepted_for_signals:false,order_actions:false};
  for(const key of ['min_queue','min_drop','window_seconds','min_traded','burst_volume'])$(key).value=rule[key];
  $('drop_pct').value=rule.drop_fraction*100;
  $('collapse').addEventListener('click',()=>{const box=shadow.querySelector('.box');box.classList.toggle('compact');
    $('collapse').textContent=box.classList.contains('compact')?'بازکردن':'جمع‌کردن';});
  function beep(){if(!audio||audio.state!=='running')return;const o=audio.createOscillator(),g=audio.createGain();
    o.connect(g);g.connect(audio.destination);g.gain.value=.09;o.frequency.value=880;o.start();o.stop(audio.currentTime+.3);}
  $('sound').addEventListener('click',async()=>{try{audio=audio||new AudioContext();await audio.resume();
    state.sound_enabled=audio.state==='running';$('sound').textContent=state.sound_enabled?'صدا فعال است':'صدا فعال نشد';
    $('test').disabled=!state.sound_enabled;if(state.sound_enabled)beep();}catch{$('settings').textContent='مرورگر اجازه پخش صدا نداد.';}});
  $('test').addEventListener('click',beep);
  function stop(){closed=true;if(timer!==null)clearTimeout(timer);engine.invalidate(initialKey);
    state.status='stopped_by_user';host.remove();delete window[slot];if(audio)audio.close().catch(()=>{});}
  $('close').addEventListener('click',stop);
  $('save').addEventListener('click',()=>{try{
    const next={...rule,sides:$('sides').value==='both'?['sell','buy']:[$('sides').value]};
    for(const key of ['min_queue','min_drop','window_seconds','min_traded','burst_volume'])next[key]=Number($(key).value);
    next.drop_fraction=Number($('drop_pct').value)/100;
    rule=validatePressureRule(next);engine.configure(rule);$('settings').textContent='شرایط اعمال شد؛ برای مقایسه، نمونه‌های تازه لازم است.';
  }catch(e){$('settings').textContent=e.message;}});
  function addEvent(e){history.unshift(e);if(history.length>15)history.pop();state.events++;
    $('events').replaceChildren();for(const item of history){const div=document.createElement('div');div.className='event';
      const heading=document.createElement('strong');heading.textContent=titles[item.kind]||item.kind;
      const text=document.createElement('div');text.textContent=item.observed_at.slice(11,19)+
        (item.queue_before===undefined?'':' | صف '+fmt(item.queue_before)+' ← '+fmt(item.queue_now))+
        (item.traded_volume===undefined?'':' | حجم همراه '+fmt(item.traded_volume))+
        (item.recent_shares_per_second===undefined?'':' | نرخ سهم/ثانیه '+fmt(Math.round(item.prior_shares_per_second))+' ← '+fmt(Math.round(item.recent_shares_per_second)))+
        (item.near_opposing_depth_change===null||item.near_opposing_depth_change===undefined?'':' | تغییر عمق مقابل '+fmt(item.near_opposing_depth_change));
      div.append(heading,text);$('events').append(div);}
    beep();
  }
  function tick(){
    if(closed)return;
    const started=performance.now(),gap=lastTick===null?null:started-lastTick;lastTick=started;
    if(gap!==null)state.max_gap_ms=Math.max(state.max_gap_ms,gap);
    try{
      if(!isBoardUrl(location.href)||new URL(location.href).searchParams.get('isin')!==initialKey){
        engine.invalidate(initialKey);state.status='board_changed';state.last_error='نشانی تابلو عوض شد؛ این پایش را دوباره باز کنید.';
        $('health').className='bad';$('health').textContent=state.last_error;return;
      }
      const received=tehranNow(),q=parseBoard(readBoardDom(),received);
      if(q.key!==initialKey||firstSymbol!==null&&q.symbol!==firstSymbol)throw new Error('هویت تابلو در این نشست تغییر کرد');
      firstSymbol=firstSymbol||q.symbol;state.symbol=q.symbol;$('symbol').textContent=q.symbol;
      const weekday=new Date(q.market_day+'T12:00:00+03:30').getUTCDay(),time=received.slice(11,19);
      const session=[6,0,1,2,3].includes(weekday)&&time>='09:00:00'&&time<'12:30:00';
      let outcome={events:[],reset:true,baseline_reason:'outside_regular_session'};
      if(session)outcome=engine.observe(q,started);else engine.invalidate(initialKey);
      state.status=session?'observed':'outside_regular_session';state.samples++;state.last_error=null;
      state.last_observed_at=q.observed_at;state.last_trade_age_seconds=q.trade_age_seconds;
      state.last_sell_queue=q.sell_queue;state.last_buy_queue=q.buy_queue;state.last_volume=q.volume;
      state.last_price_rial=q.last_price;state.last_gap_ms=gap;
      state.sound_enabled=!!audio&&audio.state==='running';
      $('sound').textContent=state.sound_enabled?'صدا فعال است':'فعال‌کردن صدا';$('test').disabled=!state.sound_enabled;
      const tradeStale=q.trade_age_seconds>rule.max_trade_age_seconds;
      $('health').className=session&&!tradeStale?'good':'bad';
      $('health').textContent=!session?'خارج از ساعات متعارف؛ مقایسه متوقف است.':
        outcome.baseline_reason==='sampling_gap'?'وقفه در نمونه‌گیری؛ مبنا از نو ساخته شد.':tradeStale?
          'آخرین معامله قدیمی است؛ هشدارهای حجم متوقف‌اند. تازگی دفتر نامعلوم است.':'خواندن تابلو برقرار است؛ تازگی خود دفتر تأیید نشده.';
      $('quote').textContent='قیمت: '+fmt(q.last_price)+' ریال | حجم روز: '+fmt(q.volume)+
        '\nصف فروش کف: '+fmt(q.sell_queue)+' | صف خرید سقف: '+fmt(q.buy_queue);
      $('fresh').textContent='عمر آخرین معامله: '+fmt(Math.floor(q.trade_age_seconds))+' ثانیه | فاصله نمونه: '+
        (gap===null?'نامعلوم':fmt(Math.round(gap))+' میلی‌ثانیه')+' | صف نامعلوم، صفر نیست.';
      for(const event of outcome.events)addEvent(event);
    }catch(e){engine.invalidate(initialKey);state.status='invalid';state.last_error=String(e?.message||e);
      $('health').className='bad';$('health').textContent='داده قابل‌اعتماد نیست؛ مقایسه متوقف شد. '+state.last_error;
      $('quote').textContent='';$('fresh').textContent='آخرین مقدار معتبر ممکن است قدیمی باشد.';}
    finally{if(!closed&&state.status!=='board_changed')timer=setTimeout(tick,Math.max(25,500-(performance.now()-started)));}
  }
  window[slot]={state,snapshot:()=>({state:{...state},rule:{...rule,sides:[...rule.sides]},events:history.map(event=>({...event}))}),stop,
    focus:()=>host.scrollIntoView({block:'nearest'})};
  tick();
  return {status:'installed',version:pressureVersion,key:initialKey};
})()
