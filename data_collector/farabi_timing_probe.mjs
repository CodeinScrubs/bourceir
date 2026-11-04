/** Session-scoped passive observation. This function has no trading/network sender. */
export function installFarabiTiming(config) {
  'use strict';
  config = config || {};
  const synthetic = config.synthetic === true;
  const allowed = synthetic
    ? location.hostname === '127.0.0.1' && location.pathname === '/tests/farabi-timing'
    : location.protocol === 'https:' && location.hostname === 'farabixo.irfarabi.com';
  if (!allowed || !/^[A-Z0-9]{12}$/.test(config.isin || '') ||
      new URL(location.href).searchParams.get('isin') !== config.isin) return {installed:false, reason:'out_of_scope'};
  if (window.__brsFarabiTimingV1?.active) return {installed:false,reason:'active_probe_already_exists',
    existing_version:window.__brsFarabiTimingV1.version};
  const capacity = Math.min(10000, Math.max(32, config.capacity || 2048));
  const keepMs = Math.min(4 * 3600000, Math.max(10000, config.lifetimeMs || 30 * 60000));
  const start = performance.now(), origin = performance.timeOrigin;
  const documentId = `page-${Math.round(origin)}-${Math.round(start)}`;
  const events = [], cleanups = [], timers = [], intents = [];
  let seq = 0, dropped = 0, acknowledged = 0, active = true, lastAck = start;
  let writerAckHealthy = false, lastBeat = start, priorWall = Date.now(), segment = 1, clockDiscontinuity=false;
  let lastOrders = '', lastForm = '', lastRowsAt = 0, intentCount = 0, formDetected = false;
  const number = value => {
    const text = String(value ?? '').trim().replace(/[۰-۹]/g,c=>String(c.charCodeAt(0)-1776))
      .replace(/[٠-٩]/g,c=>String(c.charCodeAt(0)-1632)).replace(/[,٬\s]/g,'');
    return /^\d+$/.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : null;
  };
  const emit = (type, data = {}) => {
    const record = {source_sequence:++seq, document_id:documentId, clock_segment:segment,
      page_ms:performance.now(), device_wall_ms:Date.now(), type, data, synthetic};
    if (events.length === capacity) { events.shift(); dropped++; }
    events.push(record);
    return record;
  };
  const visible = element => {
    const r = element.getBoundingClientRect(); return r.width > 0 && r.height > 0;
  };
  const currentForm = () => {
    const button = [...document.querySelectorAll('button')].find(b=>visible(b) &&
      ['ارسال خرید','ارسال فروش','ارسال و بستن'].includes(b.innerText.trim()));
    const form = button?.closest('form');
    if (!form) return null;
    const fields = [...form.querySelectorAll('input[type="tel"]')];
    const selected = [...document.querySelectorAll('[role="tab"][aria-selected="true"]')].map(t=>t.innerText.trim());
    return {isin:config.isin, side:selected.includes('خرید')?'buy':selected.includes('فروش')?'sell':null,
      ordinary:selected.includes('سفارش عادی'), quantity_shares:number(fields[0]?.value),
      price_rial:number(fields[1]?.value), day_validity:form.innerText.includes('معتبر تا امروز'),
      submit_enabled:!button.disabled, units:'rial_and_shares'};
  };
  const orderRows = () => {
    const heading = [...document.querySelectorAll('th,[role="columnheader"]')]
      .find(h=>visible(h)&&h.innerText.trim()==='وضعیت سفارش');
    const table = heading?.closest('table,[role="grid"],[role="table"]');
    if (!table) return {available:false, rows:[]};
    const state = text => ({'در صف':'queued','سفارش ویرایش شده و در صف':'queued',
      'انجام شده':'filled','انجام‌شده':'filled','انصراف':'cancelled',
      'لغو شده':'cancelled','حذف شده':'cancelled','ناموفق':'failed','منقضی':'expired',
      'ارسال به هسته':'sending','در حال ارسال':'sending','قسمتی انجام شده':'partially_filled'}[text.trim().split('\n')[0].trim()] || 'unknown');
    return {available:true, visible_rows_only:true, exchange_acceptance_time:null, queue_rank:null,
      rows:[...table.querySelectorAll('tr,[role="row"]')].filter(visible).map(row=>{
        const c = [...row.querySelectorAll('td,[role="cell"],[role="gridcell"]')].map(t=>t.innerText.trim());
        if (c.length < 7 || c[1].replace(/[\u200c\s]/g,'') !== (config.symbol || 'فکور').replace(/[\u200c\s]/g,'')) return null;
        const counts = c[2].replace(/[۰-۹]/g,ch=>String(ch.charCodeAt(0)-1776)).match(/[\d,٬]+/g)||[];
        return {side:c[0]==='خرید'?'buy':c[0]==='فروش'?'sell':null, quantity_shares:number(counts[0]),
          filled_shares:number(counts[1]), price_rial:number(c[3]), broker_state:state(c[6]),
          displayed_time:c[5].replace(/[^۰-۹٠-٩0-9\s,:/\-آ-ی]/g,'').slice(0,60),
          displayed_time_semantics:'broker_ui_unverified'};
      }).filter(Boolean).slice(0,50)};
  };
  const classify = name => {
    let url; try { url = new URL(name); } catch { return null; }
    const hosts = synthetic ? ['127.0.0.1'] : ['farabixo.irfarabi.com','gateway.farabixo.com'];
    if (!hosts.includes(url.hostname)) return null;
    const p = url.pathname;
    if (/\/orders?(?:\/|$)/i.test(p)) return {host:url.hostname, path_class:/\/orders?\/?$/i.test(p)?'orders':'order_detail'};
    if (/stockwatch|instruments|calendar/i.test(p)) return {host:url.hostname,path_class:'market_data'};
    return null;
  };
  const onClick = event => {
    if (!active || (!event.isTrusted && !synthetic)) return;
    const button = event.target.closest?.('button');
    const label = button?.innerText?.trim();
    if (!['ارسال خرید','ارسال فروش','ارسال و بستن'].includes(label)) return;
    if (new URL(location.href).searchParams.get('isin') !== config.isin) return;
    const form = currentForm(); if (!form) return;
    const id = `intent-${++intentCount}`;
    const row = emit('ui.intent', {intent_id:id, event_timestamp_ms:event.timeStamp,
      trusted:event.isTrusted, action:'submit_control', ...form});
    intents.push({id, at:row.page_ms});
    if (intents.length > 200) intents.shift();
    lastOrders = ''; lastForm = '';
  };
  document.addEventListener('click',onClick,{capture:true,passive:true});
  cleanups.push(()=>document.removeEventListener('click',onClick,true));
  const resourceObserver = new PerformanceObserver(list=>{
    if (!active) return;
    for (const entry of list.getEntries()) {
      const target = classify(entry.name); if (!target) continue;
      const candidates = intents.filter(i=>entry.startTime>=i.at-1 && entry.startTime-i.at<10000);
      emit('resource', {...target, start_ms:entry.startTime, duration_ms:entry.duration,
        response_end_ms:entry.responseEnd, request_start_ms:entry.requestStart>0?entry.requestStart:null,
        response_start_ms:entry.responseStart>0?entry.responseStart:null,
        http_status:entry.responseStatus>0?entry.responseStatus:null,
        initiator:['xmlhttprequest','fetch','script','link'].includes(entry.initiatorType)?entry.initiatorType:'other',
        candidate_intents:candidates.map(i=>i.id), correlation:'time_window_inference', method:null});
    }
  });
  resourceObserver.observe({type:'resource',buffered:false}); cleanups.push(()=>resourceObserver.disconnect());
  let badge, badgeStatus='DEGRADED';
  const localClock=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Tehran',hour12:false,
    hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3});
  const renderBadge=()=>{
    if(!badge||!active)return;
    badge.style.background=badgeStatus==='RECORDING'?'#0f6846':'#9a3e17';
    badge.textContent=`${synthetic?'SYNTHETIC | ':''}LOCAL Tehran ${localClock.format(Date.now())} | PASSIVE ${badgeStatus} | FORM UI ${formDetected?'FOUND':'NOT FOUND'} | buffered ${events.length} | lost ${dropped}`;
  };
  const mount = () => {
    if (!document.body || badge) return;
    badge = document.querySelectorAll('#brs-passive-timing-status')[0] || document.createElement('div'); badge.id = 'brs-passive-timing-status';
    badge.style.cssText='position:fixed;left:8px;bottom:8px;z-index:2147483647;padding:5px 9px;background:#302f36;color:white;font:12px sans-serif;pointer-events:none;border-radius:5px;direction:ltr';
    badge.setAttribute('aria-label','Passive recorder health'); document.body.appendChild(badge);
  };
  const onVisibility = () => emit('page.visibility',{visibility:document.visibilityState});
  const onHide = () => { emit('page.boundary',{reason:'pagehide',missing_future_possible:true}); };
  document.addEventListener('visibilitychange',onVisibility,{passive:true});
  window.addEventListener('pagehide',onHide,{passive:true});
  cleanups.push(()=>document.removeEventListener('visibilitychange',onVisibility),()=>window.removeEventListener('pagehide',onHide));
  const tick = () => {
    if (!active) return;
    const now = performance.now(), wall = Date.now(), gap = now-lastBeat;
    if (Math.abs((wall-priorWall)-gap)>100 || gap>5000) {
      clockDiscontinuity=true;
      segment++; emit('clock.discontinuity',{wall_delta_ms:wall-priorWall,monotonic_delta_ms:gap,
        uncertainty_ms:null,reason:gap>5000?'suspend_or_scheduler_gap':'wall_step'});
    }
    lastBeat=now; priorWall=wall;
    const scope = new URL(location.href).searchParams.get('isin') === config.isin;
    if (!scope) formDetected=false;
    if (scope && now-lastRowsAt>=500) {
      lastRowsAt=now;
      const form=currentForm();formDetected=!!form;
      const encoded=JSON.stringify(form);
      if (encoded!==lastForm) {lastForm=encoded;emit('ui.form',{...form,available:formDetected});}
      const rows=orderRows(), encodedRows=JSON.stringify(rows);
      if(encodedRows!==lastOrders){lastOrders=encodedRows;emit('ui.orders',rows);}
    }
    mount();
    const health = !scope || dropped || clockDiscontinuity || now-lastAck>8000 || !writerAckHealthy ? 'DEGRADED' : 'RECORDING';
    badgeStatus=health;renderBadge();
    emit('heartbeat',{health,writer_ack_age_ms:now-lastAck,last_durable_page_sequence:acknowledged,
      buffered:events.length,dropped,visibility:document.visibilityState,scope_ok:scope,
      event_loop_delay_ms:Math.max(0,gap-1000),wall_vs_origin_ms:wall-(origin+now),form_detected:formDetected});
  };
  const stop = reason => {
    if(!active)return;emit('session.stop',{reason:reason||'operator_stop'});active=false;
    cleanups.forEach(fn=>fn());timers.forEach(clearInterval);
    if(badge){badge.style.background='#302f36';badge.textContent='PASSIVE STOPPED — save pending records';}
  };
  const api = {version:'1.0.3',documentId,timeOrigin:origin,synthetic,
    get active(){return active;},
    read(after=0){
      const before=performance.now(),wall=Date.now(),afterRead=performance.now();
      return {version:this.version,document_id:documentId,time_origin:origin,synthetic,active,
        clock_sample:{page_before_ms:before,device_wall_ms:wall,page_after_ms:afterRead},
        source_sequence:seq,dropped,last_ack:acknowledged,events:events.filter(e=>e.source_sequence>after).slice(0,512)};
    },
    ack(value,healthy){if(Number.isSafeInteger(value)&&value<=seq&&value>=acknowledged){
      acknowledged=value;lastAck=performance.now();writerAckHealthy=healthy===true;
      while(events.length&&events[0].source_sequence<=value)events.shift();}return acknowledged;},stop,
    markSaved(){if(!active&&badge)badge.textContent='PASSIVE SAVED | recording stopped';}};
  window.__brsFarabiTimingV1=api;
  emit('session.start',{missing_prefix:true,isin:config.isin,scope:'selected_tab_top_frame',
    capabilities:{resources:true,clicks:true,visible_order_rows:true,network_method:false,exchange_time:false},
    wall_bracket_ms:null,utc_uncertainty_ms:null});
  tick(); timers.push(setInterval(tick,1000));
  // A 50 ms display refresh assists manual operation; it is neither an
  // exchange clock nor a 1 ms accuracy claim and never triggers an order.
  timers.push(setInterval(renderBadge,50));
  timers.push(setTimeout(()=>stop('lifetime_limit'),keepMs));
  return {installed:true,version:api.version,document_id:documentId,synthetic};
}
