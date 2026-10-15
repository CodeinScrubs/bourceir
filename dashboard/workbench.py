"""Persian manual risk planner, descriptive shortlist and local execution journal."""

import json
import uuid

import pandas as pd
import streamlit as st

from config import settings
from core import market_rules
from core.journal import summarize_executions
from core.risk import RiskProfile, plan_trade
from core.screening import current_daily_row, current_snapshot, descriptive_screen
from core.timeutils import now_tehran_iso, to_jalali
from database.broker import BrokerRepository
from database.db import DatabaseManager
from database.workbench import WorkbenchRepository

HORIZONS = {"همان روز": "intraday", "۲ تا ۳ روز": "swing", "چند هفته تا چند ماه": "weeks_months"}


def _toman(value):
    return "نامعلوم" if value is None else f"{value / 10:,.0f} تومان"


def _account_metric_style():
    st.markdown('<style>[data-testid="stMetricValue"] > div {direction:ltr; unicode-bidi:isolate; text-align:right; '
                'white-space:normal; overflow:visible; text-overflow:clip; font-size:clamp(1.1rem,2.3vw,2rem);}</style>',
                unsafe_allow_html=True)


def page_workbench():
    _account_metric_style()
    st.title("🛡️ برنامه و دفتر معاملات")
    st.caption("برنامه‌های ورود و خروج را خودت تعیین می‌کنی؛ سامانه هزینه و ریسک را محاسبه می‌کند. همه مبالغ ورودی تومان هستند.")
    with DatabaseManager(read_only=True) as db:
        repo = WorkbenchRepository(db)
        saved = repo.profile()
        rows = repo.market_rows()
        broker_snapshot = BrokerRepository(db).latest('portfolio')
        broker_baseline = BrokerRepository(db).baseline()
    with st.expander("سرمایه و سقف زیان کل", expanded=saved is None):
        with st.form("risk-profile"):
            capital = st.number_input("سرمایه مبنا (تومان)", min_value=0.1 if broker_baseline else 1,
                                      value=saved['capital_rial'] / 10 if broker_baseline else saved["capital_rial"] // 10 if saved else 1,
                                      disabled=broker_baseline is not None)
            limit = st.number_input("سقف زیان کل (تومان)", min_value=0.1 if broker_baseline else 1,
                                    value=saved['max_loss_rial'] / 10 if broker_baseline else saved["max_loss_rial"] // 10 if saved else 1,
                                    disabled=broker_baseline is not None)
            complete = st.checkbox("همه معاملات، دارایی‌ها و نقدینگی واقعی مربوط به این سرمایه را در دفتر ثبت کرده‌ام",
                                   value=bool(saved and saved["actual_history_complete"] and not broker_snapshot),
                                   disabled=broker_snapshot is not None)
            if broker_snapshot:
                st.caption('گزارش واردشده کارگزاری در صفحه حساب دیده می‌شود؛ تأیید دفتر واقعی پس از بازسازی و تطبیق موجودی ممکن می‌شود.')
            if broker_baseline:
                st.caption('مبنای دقیق مرحله فارابی در صفحه حساب ثبت شده و با این فرم یا ورود گزارش تازه تغییر نمی‌کند.')
            if st.form_submit_button("ذخیره تنظیمات", disabled=broker_baseline is not None):
                try:
                    profile = RiskProfile(capital * 10, limit * 10)
                    with DatabaseManager() as db:
                        WorkbenchRepository(db).save_profile(profile, complete)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if saved is None:
        st.info("ابتدا سرمایه و سقف زیان را ثبت کن. درصد ریسک هر معامله را خودت تعیین می‌کنی.")
        return
    profile = RiskProfile(saved["capital_rial"], saved["max_loss_rial"])
    mode_label = st.radio("دفتر", ["کاغذی", "واقعی"], horizontal=True)
    mode = "paper" if mode_label == "کاغذی" else "actual"
    with DatabaseManager(read_only=True) as db:
        repo = WorkbenchRepository(db)
        events, plans = repo.executions(mode), repo.plans(mode)
    marks = dict(zip(rows["symbol_fa"], market_rules.official_close(rows))) if not rows.empty else {}
    state = summarize_executions(events, profile, marks)
    actual_incomplete = mode == 'actual' and (broker_snapshot is not None or not saved['actual_history_complete'])
    display_cash = state['cash_rial']
    display_equity = state['equity_rial']
    if actual_incomplete:
        display_cash = broker_snapshot['summary']['buying_power_rial'] if broker_snapshot else None
        display_equity = broker_snapshot['summary']['net_assets_rial'] if broker_snapshot else None
    c1, c2 = st.columns(2)
    c3, c4 = st.columns(2)
    c1.metric("قدرت خرید آخرین گزارش" if actual_incomplete else "نقدینگی دفتر", _toman(display_cash))
    c2.metric("سود/زیان تحقق‌یافته خالص", _toman(None if actual_incomplete else state["realized_net_rial"]))
    c3.metric("ارزش آخرین گزارش" if actual_incomplete else "ارزش برآوردی دفتر", _toman(display_equity))
    c4.metric("سقف زیان کل", _toman(profile.max_loss_rial))
    st.caption("ارزش دفتر از آخرین قیمت آرشیو و کارمزد فروش برآورد می‌شود؛ تازگی قیمت و امکان فروش جدا بررسی می‌شوند.")
    if state["estimated_fee_events"]:
        st.info(f"کارمزد {state['estimated_fee_events']} اجرا از تنظیمات برآورد شده است؛ با کارمزد گزارش کارگزاری فرق دارد.")
    if state["loss_limit_reached"]:
        st.error("سقف زیان کل رسیده است: برنامه جدید متوقف است؛ روش و دفتر را بازبینی کن.")
    if actual_incomplete:
        st.warning("تا کامل بودن دفتر واقعی تأیید نشود، اندازه معامله جدید محاسبه نمی‌شود.")
    account_current = not state["unknown_marks"]
    risk_reserved = 0.0
    for symbol, pos in state["positions"].items():
        source = rows.loc[rows["symbol_fa"] == symbol]
        account_current &= not source.empty and current_daily_row(source.iloc[0]["date"])
        plans_by_id = {p["id"]: p for p in plans}
        linked = [plans_by_id.get(key) for key in pos["plan_ids"]]
        stop_price = min(p["stop_rial"] for p in linked) if linked and all(linked) else None
        marked_value = pos.get("liquidation_value_rial", pos["cost_rial"])
        slip = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10000
        stop_value = pos["quantity"] * stop_price * (1 - settings.FEES["sell"]) * (1 - slip) if stop_price else 0
        risk_reserved += max(0, marked_value - stop_value)
    if state["positions"]:
        positions = pd.DataFrame.from_dict(state["positions"], orient="index").reset_index().rename(columns={"index": "نماد"})
        positions["موجودی"] = positions.pop("quantity")
        positions["بهای تمام‌شده (تومان)"] = positions.pop("cost_rial") / 10
        positions["صنعت"] = positions["نماد"].map(dict(zip(rows["symbol_fa"], rows["sector_code"]))).fillna("نامعلوم")
        positions["درصد سرمایه مبنا"] = positions["بهای تمام‌شده (تومان)"] * 10 / profile.capital_rial * 100
        st.dataframe(positions[["نماد", "موجودی", "بهای تمام‌شده (تومان)"]], hide_index=True)
        st.dataframe(positions.groupby("صنعت", dropna=False)["درصد سرمایه مبنا"].sum().reset_index(), hide_index=True)
        st.caption("تمرکز صنعت از بهای خرید گزارش می‌شود؛ صنعت نامعلوم جدا می‌ماند. ریسک باز بدون برنامه خروج، کل ارزش پوزیشن فرض می‌شود.")
        st.caption("اقدام شرکتی و واریز/برداشت در این دفتر هنوز خودکار نیستند؛ اگر موجودی واقعی تغییر کرده، تأیید کامل بودن دفتر را بردار.")
    if rows.empty:
        st.info("برای برنامه معامله ابتدا داده‌های واقعی بازار را به‌روزرسانی کن.")
    else:
        with st.expander("محاسبه برنامه دستی", expanded=True):
            symbol = st.selectbox("نماد برنامه", sorted(rows["symbol_fa"].tolist()))
            horizon_label = st.selectbox("افق", list(HORIZONS))
            source = rows.loc[rows["symbol_fa"] == symbol].iloc[0]
            st.caption(f"آخرین روز داده: {to_jalali(source['date'])} — ورود، حد زیان و هدف را خودت وارد کن.")
            entry = st.number_input("قیمت ورود (تومان برای هر سهم)", min_value=0.1, value=max(0.1, marks[symbol] / 10))
            stop = st.number_input("قیمت سناریوی خروج با زیان (تومان)", min_value=0.1, value=0.1)
            target = st.number_input("قیمت هدف خودت (تومان)", min_value=0.1, value=0.1)
            budget = st.number_input("بودجه زیان همین معامله (تومان)", min_value=1, value=1)
            rationale = st.text_area("دلیل انتخاب سهم و شرط نامعتبر شدن برنامه")
            data_current = current_daily_row(source["date"]) and account_current
            locks = market_rules.lock_flags(pd.DataFrame([source])).iloc[0]
            data_current &= bool(market_rules.daily_vwap(pd.DataFrame([source])).notna().iloc[0])
            if HORIZONS[horizon_label] == "intraday":
                with DatabaseManager(read_only=True) as db:
                    snapshot = WorkbenchRepository(db).latest_snapshot(symbol)
                data_current &= bool(snapshot and current_snapshot(snapshot["provider_timestamp"]))
            try:
                calculation = plan_trade(profile=profile, entry_rial=entry * 10, stop_rial=stop * 10, target_rial=target * 10,
                                         trade_loss_budget_rial=budget * 10, available_cash_rial=state["cash_rial"],
                                         equity_rial=state["equity_rial"] or 0, reserved_risk_rial=risk_reserved,
                                         daily_value_rial=source["value"], data_current=bool(data_current),
                                         locked_up=bool(locks["locked_up"]), locked_down=bool(locks["locked_down"]),
                                         account_complete=mode == "paper" or not actual_incomplete)
                if calculation["blockers"]:
                    st.warning("؛ ".join(calculation["blockers"]))
                else:
                    st.write(f"حداکثر تعداد در این سناریو: {calculation['quantity']:,} سهم | "
                             f"سرمایه لازم: {_toman(calculation['entry_cost_rial'])}")
                    st.write(f"زیان سناریوی خروج: {_toman(calculation['stop_loss_scenario_rial'])} | "
                             f"سود خالص در هدف خودت: {_toman(calculation['target_net_scenario_rial'])}")
                    st.caption(f"قیمت سربه‌سر با هزینه و لغزش: {_toman(calculation['breakeven_exit_rial'])} برای هر سهم")
                    st.warning("حد زیان قیمت فروش تضمینی نیست؛ صف فروش و توقف می‌تواند زیان بیشتری بسازد. این محاسبه سیگنال خرید نیست.")
                    if st.button("ثبت برنامه دستی", disabled=not rationale.strip()):
                        calculation["price_source_date"] = source["date"]
                        calculation["liquidity_source_rial"] = float(source["value"])
                        with DatabaseManager() as db:
                            WorkbenchRepository(db).save_plan(symbol=symbol, mode=mode, horizon=HORIZONS[horizon_label],
                                                            entry_rial=entry * 10, stop_rial=stop * 10, target_rial=target * 10,
                                                            rationale=rationale, calculation=calculation)
                        st.success("برنامه ذخیره شد؛ هیچ اجرایی ثبت نشده است.")
            except ValueError as exc:
                st.info(str(exc))
    with st.expander("ثبت اجرای دستی در دفتر"):
        choices = sorted(set(rows["symbol_fa"].tolist()) | set(state["positions"])) if not rows.empty else sorted(state["positions"])
        if not choices:
            st.info("نمادی برای ثبت اجرا موجود نیست.")
        else:
            with st.form("execution", clear_on_submit=True):
                symbol = st.selectbox("نماد اجرا", choices)
                side = st.selectbox("خرید یا فروش", ["خرید", "فروش"])
                quantity = st.number_input("تعداد سهم اجراشده", min_value=1, value=1)
                price = st.number_input("قیمت اجرای خودت (تومان)", min_value=0.1, value=0.1)
                timestamp = st.text_input("زمان میلادی اجرا با منطقه زمانی تهران", value=now_tehran_iso())
                actual_fee = st.checkbox("مبلغ کارمزد دقیق را می‌دانم")
                fee = st.number_input("کارمزد کل همین اجرا (تومان)", min_value=0.0, value=0.0)
                reason = st.text_area("دلیل و توضیح اجرا")
                plan_choices = {"بدون برنامه مرتبط": None} | {
                    f"{p['symbol_fa']} — {to_jalali(p['created_at'], with_time=True)} — {p['id'][:8]}": p["id"] for p in plans}
                plan_label = st.selectbox("برنامه مرتبط با اجرا", list(plan_choices))
                if st.form_submit_button("ثبت اجرای گزارش‌شده"):
                    try:
                        with DatabaseManager() as db:
                            WorkbenchRepository(db).record_execution(mode=mode, symbol=symbol,
                                side="buy" if side == "خرید" else "sell", quantity=quantity, price_rial=price * 10,
                                executed_at=timestamp, rationale=reason, fee_rial=fee * 10 if actual_fee else None,
                                plan_id=plan_choices[plan_label],
                                execution_id=uuid.uuid4().hex)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    if plans:
        table = pd.DataFrame(plans)[["symbol_fa", "created_at", "horizon", "quantity", "rationale"]]
        table["created_at"] = table["created_at"].map(lambda value: to_jalali(value, with_time=True))
        st.dataframe(table.rename(columns={"symbol_fa": "نماد", "created_at": "زمان ثبت", "horizon": "افق",
                                          "quantity": "تعداد برنامه", "rationale": "دلیل"}), hide_index=True)
    if events:
        table = pd.DataFrame(events)
        table["زمان"] = table["executed_at"].map(lambda value: to_jalali(value, with_time=True))
        table["قیمت (تومان)"] = table["price_rial"] / 10
        table["کارمزد (تومان)"] = table["fee_rial"] / 10
        st.dataframe(table[["symbol_fa", "side", "quantity", "زمان", "قیمت (تومان)", "کارمزد (تومان)"]].rename(
            columns={"symbol_fa": "نماد", "side": "نوع اجرا", "quantity": "تعداد"}), hide_index=True)
        st.download_button("دریافت دفتر همین نوع معاملات", json.dumps(events, ensure_ascii=False, indent=2),
                           file_name=f"brs-journal-{mode}.json", mime="application/json")


def page_shortlist():
    st.title("🔎 فهرست بررسی")
    st.caption("مرتب‌سازی توصیفی بر اساس ارزش معاملات؛ احتمال سود یا توصیه ورود و خروج از این جدول استنتاج نمی‌شود.")
    with DatabaseManager(read_only=True) as db:
        rows = WorkbenchRepository(db).market_rows()
    if rows.empty:
        st.info("ابتدا داده‌های واقعی سهام را به‌روزرسانی کن.")
        return
    minimum = st.number_input("حداقل ارزش معاملات روز (میلیارد تومان)", min_value=0.0,
                              value=settings.RESEARCH_CONFIG["min_daily_value_rial"] / 1e10)
    screened = descriptive_screen(rows, minimum * 1e10)
    eligible = screened.loc[screened["current"] & screened["liquid"]]
    st.write(f"{len(eligible)} نماد با داده روز کامل و نقدشوندگی خواسته‌شده از {len(rows)} نماد دارای قیمت")
    if eligible.empty:
        st.warning("فهرست به‌روز خالی است؛ داده قدیمی یا نقدشوندگی ناکافی را فرصت معاملاتی فرض نکن.")
    show_old = st.checkbox("نمایش داده قدیمی برای بررسی توصیفی")
    table = screened.loc[screened["liquid"]] if show_old else eligible
    table = table.copy()
    table["روز داده"] = table["date"].map(to_jalali)
    table["ارزش معاملات (میلیارد تومان)"] = table["value"] / 1e10
    st.dataframe(table[["symbol_fa", "روز داده", "ارزش معاملات (میلیارد تومان)", "change_pct", "locked_up", "locked_down", "current"]].rename(
        columns={"symbol_fa": "نماد", "change_pct": "تغییر پایانی (٪)", "locked_up": "صف خرید قفل",
                 "locked_down": "صف فروش قفل", "current": "روز مورد انتظار"}), hide_index=True)
    st.info("مسیر چند هفته تا چند ماه هنوز روش پذیرفته‌شده ندارد. جهش قیمت ناشی از اقدام شرکتی، بازده قابل برداشت محسوب نمی‌شود.")
