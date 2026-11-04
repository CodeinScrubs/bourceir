/** Read-only DOM clock observation. No fetch, input, page globals, or account data. */
export function observeClockDisplay(config) {
  return new Promise(resolve => {
    const duration = config.durationMs;
    const intervalMs = config.intervalMs ?? 20;
    if (!Number.isFinite(duration) || duration < 1000 || duration > 60000 || !Number.isFinite(intervalMs) || intervalMs < 20 || intervalMs > 200 || typeof config.selector !== 'string') {
      resolve({error: 'invalid_probe_configuration'}); return;
    }
    const normalize = value => (value || '').replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 1776)).replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 1632));
    const clockPattern = /^\s*\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}(?:\s*(?:AM|PM))?\s*$/i;
    let elements;
    try { elements = Array.from(document.querySelectorAll(config.selector)).filter(e => clockPattern.test(normalize(e.textContent))); }
    catch { resolve({error: 'invalid_clock_selector'}); return; }
    if (!elements.length || elements.length > 8) { resolve({error: 'clock_selector_match_count',count: elements.length}); return; }
    const startedWall = Date.now(), startedMono = performance.now();
    const states = elements.map((element, index) => ({element,index,text:null,lastRead:null,rows:[],detached:false}));
    let polls = 0, lastPoll = startedMono, maxGap = 0, frame = null, closed = false;
    const visibilityCounts = {};
    function read(kind) {
      if (closed) return;
      const mono = performance.now(); maxGap = Math.max(maxGap, mono - lastPoll); lastPoll = mono; polls++;
      const visibility = document.visibilityState;
      visibilityCounts[visibility] = (visibilityCounts[visibility] || 0) + 1;
      for (const state of states) {
        if (!state.element.isConnected) { state.detached = true; continue; }
        const before = Date.now(), text = normalize(state.element.textContent).trim(), after = Date.now();
        if (text !== state.text && state.rows.length < 512) {
          state.rows.push({text,window_before_ms:before,window_after_ms:after,mono_ms:mono,previous_read_ms:state.lastRead,visibility,kind});
          state.text = text;
        }
        state.lastRead = after;
      }
    }
    // Read mutation results at a frame boundary to reduce transient split
    // second/minute React renders. Polls and raw records are still DOM evidence,
    // not a compositor or destination-server timestamp.
    const observer = new MutationObserver(() => {
      if (frame == null && !closed) frame = requestAnimationFrame(() => { frame = null; read('frame'); });
    });
    for (const state of states) observer.observe(state.element, {subtree:true,childList:true,characterData:true});
    read('initial');
    const timer = setInterval(() => read('poll'), intervalMs);
    setTimeout(() => {
      read('final'); closed = true; clearInterval(timer); observer.disconnect();
      if (frame != null) cancelAnimationFrame(frame);
      resolve({schema_version:1,selector:config.selector,start_windows_ms:startedWall,end_windows_ms:Date.now(),start_mono_ms:startedMono,end_mono_ms:performance.now(),polls,max_poll_gap_ms:maxGap,visibility_counts:visibilityCounts,nodes:states.map(s=>({index:s.index,detached:s.detached,truncated:s.rows.length>=512,rows:s.rows})),scope:'DOM text transition relative to host Windows clock; includes UI scheduling; no server/exchange accuracy inferred',input_sent:false,requests_sent:0});
    }, duration);
  });
}

export function normalizeClockDigits(value) {
  return String(value ?? '').replace(/[۰-۹]/g, c => String(c.charCodeAt(0)-1776)).replace(/[٠-٩]/g, c => String(c.charCodeAt(0)-1632)).replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '');
}

export function parseClockSeconds(value) {
  const match = normalizeClockDigits(value).trim().match(/^(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})\s*(AM|PM)?$/i);
  if (!match) return null;
  let hour = Number(match[1]); const minute = Number(match[2]), second = Number(match[3]);
  if (minute > 59 || second > 59 || hour > 23) return null;
  if (match[4]) { if (hour > 12) return null; hour = hour % 12 + (match[4].toUpperCase()==='PM'?12:0); }
  return hour*3600 + minute*60 + second;
}

const tehran = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Tehran',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
export function clockLabelEpoch(value, windowsMs) {
  const seconds = parseClockSeconds(value);
  if (seconds == null || !Number.isFinite(windowsMs)) return null;
  const parts = Object.fromEntries(tehran.formatToParts(new Date(windowsMs)).filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]));
  const localSeconds = parts.hour*3600 + parts.minute*60 + parts.second;
  let delta = seconds - localSeconds;
  // Nearest local day: clock offsets over 12 hours are intentionally not inferred.
  if (delta > 43200) delta -= 86400; if (delta < -43200) delta += 86400;
  return Math.floor(windowsMs/1000)*1000 + delta*1000;
}

function median(values) { const sorted=[...values].sort((a,b)=>a-b);return sorted.length?sorted.length%2?sorted[(sorted.length-1)/2]:(sorted[sorted.length/2-1]+sorted[sorted.length/2])/2:null; }
export function analyzeDisplayNode(node) {
  const parsed = (node.rows ?? []).map(row=>({...row,label_ms:clockLabelEpoch(row.text,row.window_after_ms)}));
  const stable = [], transient = [];
  // Preserve raw observations elsewhere. Only summarize labels that survived
  // at least 50 ms before the next change; drop the left-censored first row.
  for (let i=0;i<parsed.length;i++) {
    if (parsed[i].label_ms==null) continue;
    if (i+1<parsed.length && parsed[i+1].window_after_ms-parsed[i].window_after_ms<50) {transient.push(parsed[i]);continue;}
    stable.push(parsed[i]);
  }
  const transitions=[], discontinuities=[];
  for(let i=1;i<stable.length;i++) {
    const previous=stable[i-1],row=stable[i],labelDelta=row.label_ms-previous.label_ms;
    const entry={label:row.text,windows_ms:row.window_after_ms,label_delta_ms:labelDelta,interval_ms:row.window_after_ms-previous.window_after_ms};
    if(labelDelta!==1000){discontinuities.push(entry);continue;}
    transitions.push({...entry,appearance_lag_ms:row.window_after_ms-row.label_ms,dom_observation_bracket_ms:row.previous_read_ms==null?null:row.window_after_ms-row.previous_read_ms});
  }
  const lags=transitions.map(r=>r.appearance_lag_ms),brackets=transitions.map(r=>r.dom_observation_bracket_ms).filter(Number.isFinite);
  return {index:node.index,raw_changes:parsed.length,stable_changes:stable.length,transient_labels_excluded:transient.length,normal_second_transitions:transitions.length,appearance_lag_median_ms:median(lags),appearance_lag_min_ms:lags.length?Math.min(...lags):null,appearance_lag_max_ms:lags.length?Math.max(...lags):null,observation_bracket_max_ms:brackets.length?Math.max(...brackets):null,discontinuities,transitions,detached:node.detached??null,truncated:node.truncated??null,interpretation:'positive appearance lag means a newly shown labeled second appeared after that Windows second began; this is display behavior, not isolated server offset'};
}
