/** Observable queue changes only: no order placement, prediction, or profit claim. */
export const pressureVersion='visible-board-pressure-v5';
export const pressureDefaults=Object.freeze({
  sides:['sell','buy'],window_seconds:60,min_queue:1000000,min_drop:100000,drop_fraction:0.3,
  min_traded:100000,burst_volume:1000000,burst_seconds:20,baseline_seconds:40,acceleration_factor:2,
  depth_fraction:0.02,min_depth_increase:100000,max_gap_seconds:3,max_trade_age_seconds:45,cooldown_seconds:60,
});
const count=(n,min=0)=>Number.isSafeInteger(n)&&n>=min;
const finite=(n,min,max)=>typeof n==='number'&&Number.isFinite(n)&&n>=min&&n<=max;

export function validatePressureRule(input={}){
  if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).some(k=>!(k in pressureDefaults)))
    throw new Error('تنظیم هشدار نامعتبر است');
  const r={...pressureDefaults,...input};
  for(const k of ['min_queue','min_drop','min_traded','burst_volume','min_depth_increase'])
    if(!count(r[k],1))throw new Error('حجم باید تعداد صحیح مثبت سهم باشد');
  for(const k of ['window_seconds','burst_seconds','baseline_seconds','max_gap_seconds','max_trade_age_seconds','cooldown_seconds'])
    if(!finite(r[k],0.1,3600))throw new Error('بازه زمانی هشدار نامعتبر است');
  if(r.max_gap_seconds>Math.min(r.window_seconds,r.burst_seconds,r.baseline_seconds))
    throw new Error('شکاف مجاز از پنجره هشدار بزرگ‌تر است');
  if(!finite(r.drop_fraction,0.001,1)||!finite(r.depth_fraction,0,0.2)||!finite(r.acceleration_factor,1.01,100))
    throw new Error('نسبت هشدار نامعتبر است');
  if(!Array.isArray(r.sides)||!r.sides.length||new Set(r.sides).size!==r.sides.length||r.sides.some(x=>!['sell','buy'].includes(x)))
    throw new Error('سمت صف نامعتبر است');
  return {...r,sides:[...r.sides]};
}

// Five visible levels only. An entirely absent side and rounded quantities are unknown.
export function nearLimitDepth(q,side,fraction){
  if(!Array.isArray(q.rows)||q.rows.length!==5||!['bid','ask'].includes(side))return null;
  let total=0,sawLevel=false;
  for(const row of q.rows){
    if(!row||typeof row!=='object'||!(side in row))return null;
    const level=row[side];if(level===null)continue;
    if(!level||!count(level.price,1)||typeof level.within_band!=='boolean')return null;
    if(!level.within_band)continue;
    if(level.price<q.low_limit||level.price>q.high_limit)return null;
    sawLevel=true;
    const near=side==='bid'?level.price<=q.low_limit*(1+fraction):level.price>=q.high_limit*(1-fraction);
    if(!near)continue;
    if(!count(level.volume,1)||level.volume_precision!=='integer')return null;
    total+=level.volume;if(!Number.isSafeInteger(total))return null;
  }
  return sawLevel?total:null;
}

export class BoardPressureAlerts{
  constructor(rule={}){this.rule=validatePressureRule(rule);this.states=new Map();}
  configure(rule){this.rule=validatePressureRule(rule);this.states.clear();}
  invalidate(key){this.states.delete(key);}
  observe(q,ms){
    if(!q||typeof q.key!=='string'||!/^\w{12}$/.test(q.key)||typeof q.symbol!=='string'||!q.symbol
      ||!/^\d{4}-\d{2}-\d{2}$/.test(q.market_day||'')||typeof q.observed_at!=='string'
      ||!count(q.volume)||!count(q.last_price,1)||!count(q.low_limit,1)||!count(q.high_limit,1)
      ||q.low_limit>q.high_limit||q.last_price<q.low_limit||q.last_price>q.high_limit||!finite(ms,0,Number.MAX_SAFE_INTEGER))
      throw new Error('مشاهده معتبر تابلو لازم است');
    for(const k of ['sell_queue','buy_queue'])if(q[k]!==null&&!count(q[k]))throw new Error('صف نامعلوم صفر نیست');
    const r=this.rule,old=this.states.get(q.key);
    if(old&&ms<=old.last.ms)throw new Error('ترتیب زمان نمونه نامعتبر است');
    const dayChange=old&&q.market_day!==old.last.q.market_day;
    const bandChange=old&&(q.low_limit!==old.last.q.low_limit||q.high_limit!==old.last.q.high_limit);
    const identityChange=old&&q.symbol!==old.last.q.symbol;
    const volumeDecrease=old&&q.volume<old.last.q.volume;
    const gap=old&&ms-old.last.ms>r.max_gap_seconds*1000;
    const reset=!old||dayChange||bandChange||identityChange||volumeDecrease||gap;
    const state=reset?{history:[],last:null,latched:new Set(),cooldown:new Map(),volumeEpoch:0,volumeFresh:false}:old;
    const events=[];
    const fresh=finite(q.trade_age_seconds,0,r.max_trade_age_seconds);
    if(fresh&&!state.volumeFresh)state.volumeEpoch++;
    state.volumeFresh=fresh;
    if(!fresh){
      state.latched.delete('trade_volume_burst');state.latched.delete('trade_volume_acceleration');
      for(const side of r.sides)state.latched.delete(side+'_queue_weakening_with_trades');
    }
    const sample={q:{...q},ms,volumeEpoch:fresh?state.volumeEpoch:null};
    const cutoff=ms-Math.max(r.window_seconds,r.burst_seconds+r.baseline_seconds)*1000-r.max_gap_seconds*1000;
    state.history=state.history.filter(x=>x.ms>=cutoff);
    const emit=(kind,metrics)=>{
      const prior=state.cooldown.get(kind);
      if(prior!==undefined&&ms-prior<r.cooldown_seconds*1000)return false;
      state.cooldown.set(kind,ms);
      events.push({version:pressureVersion,kind,key:q.key,symbol:q.symbol,observed_at:q.observed_at,
        source:'farabi-visible-dom',independent_book_clock_verified:false,accepted_for_signals:false,...metrics});
      return true;
    };
    if(!reset){
      for(const side of r.sides){
        const field=side+'_queue',current=q[field],previous=state.last.q[field],kind=side+'_queue_weakening';
        if(current===null){
          // A valid, fully visible side disappearing is attention only; it is not queue=0.
          const bookSide=side==='sell'?'ask':'bid',other=side==='sell'?'bid':'ask';
          const empty=Array.isArray(q.rows)&&q.rows.length===5&&q.rows.every(row=>row&&row[bookSide]===null);
          const opposite=empty&&q.rows.some(row=>row[other]&&row[other].within_band===true);
          if(previous!==null&&previous>=r.min_queue&&empty&&opposite)
            emit(side+'_side_empty',{queue_before:previous,queue_now:null,break_proven:false,
              interpretation:'visible_side_empty_not_independent_confirmation'});
          state.history=state.history.map(x=>({...x,q:{...x.q,[field]:null}}));
          state.latched.delete(kind);state.latched.delete(kind+'_with_trades');continue;
        }
        const candidates=state.history.filter(x=>x.ms>=ms-r.window_seconds*1000&&x.q[field]!==null&&x.q[field]>=r.min_queue);
        const anchor=candidates.reduce((a,b)=>!a||b.q[field]>a.q[field]?b:a,null);
        const decrease=anchor?anchor.q[field]-current:0;
        const qualifies=!!anchor&&decrease>=r.min_drop&&decrease/anchor.q[field]>=r.drop_fraction;
        if(!qualifies){state.latched.delete(kind);state.latched.delete(kind+'_with_trades');}
        if(qualifies){
          const volumeAnchor=fresh&&state.history.find(x=>x.ms>=anchor.ms&&x.volumeEpoch===state.volumeEpoch);
          const volumeComparable=!!volumeAnchor;
          const traded=volumeComparable?q.volume-volumeAnchor.q.volume:null,opposite=side==='sell'?'bid':'ask';
          const beforeDepth=nearLimitDepth(anchor.q,opposite,r.depth_fraction),nowDepth=nearLimitDepth(q,opposite,r.depth_fraction);
          const depthChange=beforeDepth===null||nowDepth===null?null:nowDepth-beforeDepth;
          const metrics={queue_before:anchor.q[field],queue_now:current,queue_decrease:decrease,
            interval_seconds:(ms-anchor.ms)/1000,anchor_at:anchor.q.observed_at,traded_volume:traded,
            trade_anchor_at:volumeComparable?volumeAnchor.q.observed_at:null,
            traded_interval_seconds:volumeComparable?(ms-volumeAnchor.ms)/1000:null,
            last_trade_age_seconds:q.trade_age_seconds??null,trade_counter_fresh:fresh,trade_delta_usable:volumeComparable,
            near_opposing_depth_before:beforeDepth,near_opposing_depth_now:nowDepth,near_opposing_depth_change:depthChange,
            opposing_depth_increased:depthChange===null?null:depthChange>=r.min_depth_increase,
            attribution:'trades_cancellations_and_new_orders_not_separable',last_price_rial:q.last_price};
          if(!state.latched.has(kind)&&emit(kind,metrics))state.latched.add(kind);
          if(volumeComparable&&traded>=r.min_traded&&!state.latched.has(kind+'_with_trades')){
            if(emit(kind+'_with_trades',metrics))state.latched.add(kind+'_with_trades');
          }
        }
        if(previous!==null&&previous>0&&current===0){
          const bookSide=side==='sell'?'ask':'bid',top=q.rows?.[0]?.[bookSide];
          if(top&&top.within_band===true&&(side==='sell'?top.price>q.low_limit:top.price<q.high_limit))
            emit(side+'_quote_left_limit',{queue_before:previous,queue_now:0,top_price_rial:top.price,
              interpretation:'visible_best_quote_moved_from_limit_not_fill_or_reversal'});
        }
      }
      const burstAnchor=fresh&&state.history.find(x=>x.ms>=ms-r.burst_seconds*1000&&x.volumeEpoch===state.volumeEpoch);
      const burst=burstAnchor&&fresh&&q.volume-burstAnchor.q.volume>=r.burst_volume;
      if(!burst)state.latched.delete('trade_volume_burst');
      if(burst&&!state.latched.has('trade_volume_burst')){
        if(emit('trade_volume_burst',{traded_volume:q.volume-burstAnchor.q.volume,
          interval_seconds:(ms-burstAnchor.ms)/1000,last_trade_age_seconds:q.trade_age_seconds,
          interpretation:'turnover_change_does_not_identify_aggressor_side'}))state.latched.add('trade_volume_burst');
      }
      // Compare two non-overlapping observed windows; zero baseline stays unknown.
      const midTarget=ms-r.burst_seconds*1000;
      const mid=fresh&&[...state.history].reverse().find(x=>x.volumeEpoch===state.volumeEpoch&&
        x.ms<=midTarget&&midTarget-x.ms<=r.max_gap_seconds*1000);
      const baseTarget=mid&&mid.ms-r.baseline_seconds*1000;
      const base=mid&&[...state.history].reverse().find(x=>x.volumeEpoch===state.volumeEpoch&&
        x.ms<=baseTarget&&baseTarget-x.ms<=r.max_gap_seconds*1000);
      let accelerated=false;
      if(mid&&base&&fresh){
        const recent=q.volume-mid.q.volume,prior=mid.q.volume-base.q.volume;
        const recentSeconds=(ms-mid.ms)/1000,priorSeconds=(mid.ms-base.ms)/1000;
        if(prior>0&&recent>=r.min_traded){
          const recentRate=recent/recentSeconds,priorRate=prior/priorSeconds;
          accelerated=recentRate>=priorRate*r.acceleration_factor;
          if(accelerated&&!state.latched.has('trade_volume_acceleration')&&
            emit('trade_volume_acceleration',{recent_traded:recent,recent_seconds:recentSeconds,prior_traded:prior,
              prior_seconds:priorSeconds,recent_shares_per_second:recentRate,prior_shares_per_second:priorRate,
              rate_ratio:recentRate/priorRate,last_trade_age_seconds:q.trade_age_seconds,
              interpretation:'turnover_change_does_not_identify_aggressor_side'}))state.latched.add('trade_volume_acceleration');
        }
      }
      if(!accelerated)state.latched.delete('trade_volume_acceleration');
    }
    state.history.push(sample);state.last=sample;this.states.set(q.key,state);
    return {events,reset,baseline_reason:reset?(!old?'first_observation':dayChange?'new_session':volumeDecrease?'counter_decreased':
      bandChange?'price_band_changed':identityChange?'identity_changed':'sampling_gap'):null};
  }
}
