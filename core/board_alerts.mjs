/** قواعد توصیفی تابلو؛ نه سیگنال خرید/فروش و نه تخمین احتمال سود. */
export const version = 'visible-board-alerts-v1';
export const defaults = Object.freeze({window_seconds:120, queue_start:5000000, queue_remaining:1000000,
  drop_fraction:0.8, min_traded:1000000, burst_seconds:60, burst_volume:1000000,
  max_gap_seconds:3, max_trade_age_seconds:45, cooldown_seconds:60, sides:['sell'], price_above:null, price_below:null});

const integer = (x, low=0) => Number.isSafeInteger(x) && x >= low;
export function validateRule(input={}) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(k=>!(k in defaults)))
    throw new Error('کلید تنظیمات هشدار نامعتبر است');
  const r={...defaults,...input};
  for (const k of ['queue_start','queue_remaining','min_traded','burst_volume'])
    if (!integer(r[k], k==='queue_remaining'?0:1)) throw new Error('آستانه حجم باید تعداد سهم صحیح باشد');
  if (r.queue_start<=r.queue_remaining) throw new Error('صف شروع باید از صف باقی‌مانده بیشتر باشد');
  for (const k of ['window_seconds','burst_seconds','max_gap_seconds','max_trade_age_seconds','cooldown_seconds'])
    if (!Number.isFinite(r[k]) || typeof r[k]!=='number' || r[k]<0.1 || r[k]>3600)
      throw new Error('بازه زمانی معتبر لازم است');
  if (r.max_gap_seconds>r.window_seconds || r.max_gap_seconds>r.burst_seconds)
    throw new Error('شکاف مجاز باید از پنجره هشدار کوتاه‌تر باشد');
  if (typeof r.drop_fraction!=='number' || !Number.isFinite(r.drop_fraction) || r.drop_fraction<=0 || r.drop_fraction>1)
    throw new Error('نسبت کاهش باید بین صفر و یک باشد');
  if (!Array.isArray(r.sides) || !r.sides.length || new Set(r.sides).size!==r.sides.length || r.sides.some(s=>!['buy','sell'].includes(s)))
    throw new Error('سمت صف نامعتبر است');
  for (const k of ['price_above','price_below']) if (r[k]!==null && !integer(r[k],1))
    throw new Error('سطح قیمت باید ریال صحیح یا نامعلوم باشد');
  return {...r,sides:[...r.sides]};
}

export class BoardAlerts {
  constructor(rule={}) { this.rule=validateRule(rule); this.states=new Map(); this.overrides=new Map(); }
  configure(rule) { this.rule=validateRule(rule); this.states.clear(); }
  configureForKey(key,rule) {
    if(typeof key!=='string'||!/^[A-Z0-9]{12}$/.test(key))throw new Error('شناسه تنظیمات نماد معتبر نیست');
    if(rule===null)this.overrides.delete(key);else this.overrides.set(key,validateRule(rule));
    this.states.delete(key);
  }
  invalidate(key) { this.states.delete(key); }
  observe(q, monotonicMs) {
    if (!q || typeof q.key!=='string' || !q.key || !integer(q.volume) || !integer(q.last_price,1)
      || !/^\d{4}-\d{2}-\d{2}$/.test(q.market_day||'') || typeof monotonicMs!=='number' || !Number.isFinite(monotonicMs))
      throw new Error('مشاهده معتبر تابلو لازم است');
    for (const k of ['sell_queue','buy_queue']) if (q[k]!==null && !integer(q[k]))
      throw new Error('صف نامعلوم صفر نیست');
    const r=this.overrides.get(q.key)||this.rule, old=this.states.get(q.key);
    if (old && monotonicMs<=old.last.ms) throw new Error('زمان نمونه باید پیوسته و صعودی باشد');
    const bandChanged=old&&(q.low_limit!==old.last.q.low_limit||q.high_limit!==old.last.q.high_limit);
    const identityChanged=old&&q.symbol!==old.last.q.symbol;
    const reset=!old || q.market_day!==old.last.q.market_day || q.volume<old.last.q.volume || bandChanged || identityChanged
      || monotonicMs-old.last.ms>r.max_gap_seconds*1000;
    const state=reset?{history:[],cooldown:new Map(),latched:new Set(),last:null}:old;
    const sample={q:{...q},ms:monotonicMs}, events=[];
    const volumeFresh=typeof q.trade_age_seconds==='number'&&Number.isFinite(q.trade_age_seconds)
      &&q.trade_age_seconds>=0&&q.trade_age_seconds<=r.max_trade_age_seconds;
    const cutoff=monotonicMs-Math.max(r.window_seconds,r.burst_seconds)*1000;
    state.history=state.history.filter(h=>h.ms>=cutoff);
    const emit=(kind,metrics)=>{
      const last=state.cooldown.get(kind);
      if (last!==undefined && monotonicMs-last<r.cooldown_seconds*1000) return;
      state.cooldown.set(kind,monotonicMs);
      events.push({version,kind,symbol:q.symbol,key:q.key,observed_at:q.observed_at,
        source:'farabi-visible-dom',independent_book_clock_verified:false,accepted_for_signals:false,...metrics});
    };
    if (!reset) {
      for (const side of r.sides) {
        const key=side+'_queue', current=q[key];
        if (current===null) {
          // گم‌شدن دفتر، شکست صف نیست؛ زنجیره این سمت از نو شروع می‌شود.
          state.history=state.history.map(h=>({...h,q:{...h.q,[key]:null}}));
          for(const kind of [side+'_queue_drop',side+'_queue_drop_with_volume'])state.latched.delete(kind);
          continue;
        }
        if (current>r.queue_remaining) for(const kind of [side+'_queue_drop',side+'_queue_drop_with_volume']) state.latched.delete(kind);
        const candidates=state.history.filter(h=>h.ms>=monotonicMs-r.window_seconds*1000 && h.q[key]!==null && h.q[key]>=r.queue_start);
        const anchor=candidates.reduce((a,b)=>!a||b.q[key]>a.q[key]?b:a,null);
        if (anchor && current<=r.queue_remaining && (anchor.q[key]-current)/anchor.q[key]>=r.drop_fraction) {
          const drop=anchor.q[key]-current, traded=q.volume-anchor.q.volume;
          const metrics={queue_before:anchor.q[key],queue_now:current,queue_decrease:drop,traded_volume:traded,volume_condition_met:traded>=r.min_traded&&volumeFresh,
            last_trade_age_seconds:q.trade_age_seconds??null,
            interval_seconds:(monotonicMs-anchor.ms)/1000,anchor_at:anchor.q.observed_at,last_price_rial:q.last_price,
            queue_decrease_exceeding_reported_volume:Math.max(0,drop-traded),
            attribution:'executions_cancellations_new_orders_and_feed_timing_not_separable'};
          for(const kind of [side+'_queue_drop',...(traded>=r.min_traded&&volumeFresh?[side+'_queue_drop_with_volume']:[])]) {
            if(!state.latched.has(kind)){emit(kind,metrics);state.latched.add(kind);}
          }
        }
      }
      const base=state.history.find(h=>h.ms>=monotonicMs-r.burst_seconds*1000);
      const burst=base && volumeFresh && q.volume-base.q.volume>=r.burst_volume;
      if (!burst) state.latched.delete('volume_burst');
      if (burst && !state.latched.has('volume_burst')) {
        emit('volume_burst',{traded_volume:q.volume-base.q.volume,interval_seconds:(monotonicMs-base.ms)/1000,
          anchor_at:base.q.observed_at,last_price_rial:q.last_price});state.latched.add('volume_burst');
      }
      for(const [name,level,above] of [['price_above',r.price_above,true],['price_below',r.price_below,false]])
        if(level!==null && (above?q.last_price>=level&&state.last.q.last_price<level:q.last_price<=level&&state.last.q.last_price>level))
          emit(name,{level_rial:level,previous_price_rial:state.last.q.last_price,last_price_rial:q.last_price});
    }
    state.history.push(sample);state.last=sample;this.states.set(q.key,state);
    return {events,reset,baseline_reason:reset?(!old?'first_observation':q.market_day!==old.last.q.market_day?'new_session':q.volume<old.last.q.volume?'counter_decreased':bandChanged?'price_band_changed':identityChanged?'identity_changed':'sampling_gap'):null};
  }
}
