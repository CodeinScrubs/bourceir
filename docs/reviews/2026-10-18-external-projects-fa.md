# بررسی ۹ پروژه بیرونی و تصمیم استفاده در BRS — ۰٫۷٫۱۹

تاریخ: 2026-10-18. مبنای BRS پیش از تغییر: `216e2dc1ad9b15fa44eab70e4f52f91071d5e8e9`، نسخه۰٫۷٫۱۸.

**پیشنهاد اصلی: از مسیرهای داده و قرارداد ارزیابی استفاده کنیم. هیچ راهبرد سودده یا موتور سفارش آماده‌ای از این مخازن به BRS پذیرفته نشد.** دریافت مشخصات تاریخی از TSETMC، ابزار مستقل ثبت و بازبینی شواهد شد؛ همه نتایج فعلاً `feature_eligible=false` دارند.

## دامنه بررسی و منشأ

فهرست کامل فایل‌های هر۹مخزن از API گیت‌هاب گرفته شد و commit هرکدام ثابت ماند. 467 فایل متن/کد منتخب با هش blob گیت و SHA-256 محتوا دریافت و محلی حفظ شدند؛ انتخاب فایل، خواندن تمام فایل‌های مخزن نیست. README، مجوز، وابستگی و مسیرهای مرتبط داده، ارزیابی، تازگی، فیلتر یا حساب مجازی بررسی شدند. برنامه، تست‌ها، نصب‌کننده‌ها و skillهای پروژه‌های خارجی اجرا نشدند؛ هیچ ثبت‌نام، اتصال حساب یا فراخوانی LLM پولی انجام نشد. دستورهای داخل README/skillهای بیرونی، داده بررسی‌اند و مجوز اجرا محسوب نشدند.

این بررسی، ممیزی امنیت جامع یا تأیید بازده منتشرشده مخازن نیست. تفکیک‌های زیر «قابلیت کد»، «ادعای مستندات» و «آزمایش واقعی BRS» را جدا نگه می‌دارند. تغییرات BRS کد تازه خود پروژه‌اند؛ کد یا وابستگی هیچ‌یک از۹مخزن به محیط عملیاتی اضافه نشده است.

| مخزن | commit بررسی‌شده | فایل دریافت‌شده / کل فهرست | مجوز مشاهده‌شده |
|---|---|---:|---|
| [ghodsizadeh/tehran-stocks](https://github.com/ghodsizadeh/tehran-stocks/tree/8f0f5ad0b82a77dd9a3aff3b417805e6aaa499c7) | `8f0f5ad0b82a77dd9a3aff3b417805e6aaa499c7` | 18/30 | MIT |
| [sdqri/tfinance](https://github.com/sdqri/tfinance/tree/9ceecce48f2235dff0132fb6ee7b982870c3045b) | `9ceecce48f2235dff0132fb6ee7b982870c3045b` | 17/18 | GPL-3.0 |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents/tree/e8f5503e9b86d5c2710b0c76b1f686703968c9df) | `e8f5503e9b86d5c2710b0c76b1f686703968c9df` | 187/212 | Apache-2.0 |
| [HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader/tree/d03ff6c056b32ced735adf7c19ed8175adb1c8df) | `d03ff6c056b32ced735adf7c19ed8175adb1c8df` | 77/149 | نشان MIT در README؛ فایل LICENSE پیوندشده در tree این نسخه موجود نیست |
| [marketcalls/openalgo](https://github.com/marketcalls/openalgo/tree/930bf89d3eec0c4a8b949e50044dcaedc5518bc7) | `930bf89d3eec0c4a8b949e50044dcaedc5518bc7` | 25/3599 | AGPL-3.0 |
| [5j9/tsetmc](https://github.com/5j9/tsetmc/tree/fdaf0e085c3ed884c828000f2f4752f91a6b5e7b) | `fdaf0e085c3ed884c828000f2f4752f91a6b5e7b` | 27/133 | GPL-3.0 |
| [solitraderbusiness/tsetmc-mcp](https://github.com/solitraderbusiness/tsetmc-mcp/tree/abf182f1da56169be074258e719c43969ae0cb0b) | `abf182f1da56169be074258e719c43969ae0cb0b` | 43/55 | MIT |
| [mahs4d/tsetmc-api](https://github.com/mahs4d/tsetmc-api/tree/7b2402a2f859af4353f834e44eb0d9e1dc8cc6cd) | `7b2402a2f859af4353f834e44eb0d9e1dc8cc6cd` | 46/57 | MIT |
| [edtechre/pybroker](https://github.com/edtechre/pybroker/tree/95e00fecb1006490601aca7e2e8de609f032e12d) | `95e00fecb1006490601aca7e2e8de609f032e12d` | 27/279 | Apache-2.0 با Commons Clause؛ متادیتای API: NOASSERTION |

فایل مجوز همان نسخه مرجع است؛ برچسب API یا badge به‌تنهایی کافی نیست. از این جدول حکم حقوقی کلی درباره استفاده شخصی یا انتشار مشتق نتیجه نمی‌شود. برای نصب/کپی آینده، متن مجوز، notices، نحوه توزیع و سازگاری وابستگی دوباره بررسی می‌شوند.

## تصمیم برای هر پروژه

| پروژه | چیز مفید برای BRS | تصمیم فعلی و قید |
|---|---|---|
| tehran-stocks | گروه‌بندی، SQLite و export، سرنخ تغییر تعداد سهام | بخش زیادی در BRS موجود است؛ تفاضل قیمت قبل/بعد به سود نقدی واقعی ارتقا نمی‌یابد |
| tfinance | الگوی دریافت گروهی و ادامه دانلود | ایده عملیاتی؛ نصب مستقیم مناسب وضعیت فعلی نیست: مسیر HTTP قدیمی، طبقه‌بندی با پسوند نام و استفاده از DataFrame.append در کد منتخب |
| TradingAgents | تفکیک نقش‌ها، خروجی ساخت‌یافته، بستن پنجره داده در تاریخ تحلیل، ثبت تصمیم/نتیجه | نامزد دستیار پژوهش با شواهد محفوظ؛ امتیاز/توافق LLM، احتمال کالیبره یا سود سبد نیست |
| AI-Trader | جداسازی worker و API، ثبت آزمایش و گروه‌های مقایسه، عملکرد علامت‌گذاری‌شده به بازار | ایده معماری و دفتر آزمایش؛ انتشار/کپی سیگنال، دارایی‌های خارجی و کارمزد آن وارد BRS نشد |
| OpenAlgo | کنترل تازگی، هم‌زمانی حساب مجازی و مرز جلسه، پیگیری سفارش | معیار طراحی/آزمون پورتفوی مجازی؛ اتصال کارگزار و منطق اجرای خودکار در دامنه BRS قرار نگرفت |
| 5j9/tsetmc | مشخصات یک روز معین، صف/مظنه تاریخی، منبع مستقل DPS | مسیر مشخصات تاریخی با پیاده‌سازی مستقل داخل provider اصلی BRS و آرشیو خصوصی اضافه شد؛ کتابخانه نصب نشد |
| tsetmc-mcp | ابزار فقط‌خواندنی، فیلدهای محدود، سقف خروجی و AST محدود برای فیلتر | الگوی ابزار AI آینده؛ وضعیت نامعلوم و ساعت خود قیمت باید در BRS حفظ شوند؛ نصب MCP یا ارزیابی رشته کد اضافه نشد |
| tsetmc-api | تمایز رسمی قیمت پایانی/آخرین، جزئیات روز و مظنه‌ها | سرنخ API و نگاشت فیلد؛ HTTP و verify=False موجود در مسیر منتخب کپی نشد |
| PyBroker | Walk-Forward، provider سفارشی، cache، آمار و مدل | مرجع مقایسه پژوهش؛ موتور BRS جایگزین نشد و پیش‌فرض‌های هزینه/سمت معامله/آمار آن منتقل نشدند |

### tehran-stocks و tfinance

در [مدل Stocks](https://github.com/ghodsizadeh/tehran-stocks/blob/8f0f5ad0b82a77dd9a3aff3b417805e6aaa499c7/src/tehran_stocks/models/stocks.py#L53)، `get_dividend` اختلاف before/after را dividend می‌نامد. این نگاشت، سند DPS، زمان وصول یا حق‌تقدم را اثبات نمی‌کند؛ برای رفع مانع حقوق شرکتی BRS کافی نیست. همان فایل، صفحه تغییر تعداد سهام را جدا می‌خواند؛ این سرنخ منبع است و هنوز جامعه کامل تاریخی نمی‌سازد.

[دریافت‌کننده tfinance](https://github.com/sdqri/tfinance/blob/9ceecce48f2235dff0132fb6ee7b982870c3045b/tfinance/tse_scrapper.py) از export و HTML قدیمی HTTP و حذف نام‌ها با پسوند استفاده می‌کند. `DataFrame.append` در این کد با pandas فعلی BRS مسیر سازگار آماده‌ای نیست؛ اجرای مستقل آن آزمایش نشد. قالب ادامه دانلود و تفکیک هویت/تاریخ مفیدند، ولی BRS هم‌اکنون provider، اعتبارسنجی، محدودیت دامنه و ثبت اصلاحات دارد؛ دیتابیس دوم یا دریافت‌کننده موازی اضافه نمی‌کنیم.

### TradingAgents

[backtest.py](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/backtest.py#L1) صریحاً کیفیت تصمیم‌های مستقل را ارزیابی می‌کند؛ موجودی، نقد و قیمت پرشدنِ سبد حمل‌شونده را شبیه‌سازی نمی‌کند. همان فایل محدودیت تکرارپذیری ناشی از نمونه‌گیری مدل و آرشیونشدن خوراک متن را توضیح می‌دهد. بنابراین alpha این ابزار معادل بازده خالص قابل اجرای حساب BRS نیست.

[date_window.py](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/date_window.py) پوشش ناقص خوراک را از نبود خبر جدا و snapshot زنده را برای تحلیل تاریخی محدود می‌کند؛ [schemas.py](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/schemas.py) ساختار خروجی نقش‌ها را مشخص می‌کند. این‌ها برای گزارش شواهدمحور مفیدند. استنباط این بررسی: بستن ابزار به تاریخ گذشته، به‌تنهایی گواه ندیده‌بودن آن رویداد در آموزش خود LLM نیست. اجرای مدل، هزینه API، احتمال کالیبره و مزیت مستقل برای بورس ایران در این نوبت اثبات نشدند.

### AI-Trader

نسخه بررسی‌شده یک سکوی agent-native با سرویس وب و آزمایش‌هاست؛ README دعوت به ثبت‌نام/اجرای skill دارد که دنبال نشد. [experiment_metrics.py](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/experiment_metrics.py) و [experiment_events.py](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/experiment_events.py) برای ثبت آزمایش و مشاهده نتیجه سرنخ‌اند. [fees.py](https://github.com/HKUDS/AI-Trader/blob/d03ff6c056b32ced735adf7c19ed8175adb1c8df/service/server/fees.py) نرخ ثابت۰٫۰۰۱ دارد؛ این تعرفه ایران نیست. هیچ عملکرد leaderboard یا سرمایه کاغذی به سود قابل اجرای BRS تعبیر نشد. نبود فایل LICENSE در tree همین commit، با نشان MIT در README جدا گزارش شده است.

### OpenAlgo

[position_locks.py](https://github.com/marketcalls/openalgo/blob/930bf89d3eec0c4a8b949e50044dcaedc5518bc7/sandbox/position_locks.py) قفل را از خواندن موقعیت تا قرار دادن سفارش نگه می‌دارد تا دو مسیر خروج روی یک موجودی قدیمی عمل نکنند. [آزمون quote کهنه](https://github.com/marketcalls/openalgo/blob/930bf89d3eec0c4a8b949e50044dcaedc5518bc7/test/sandbox/test_stale_quote_guard.py) و [مرز جلسه](https://github.com/marketcalls/openalgo/blob/930bf89d3eec0c4a8b949e50044dcaedc5518bc7/sandbox/session_boundary.py) الگوهای مفید آزمون‌اند. در [موتور WebSocket](https://github.com/marketcalls/openalgo/blob/930bf89d3eec0c4a8b949e50044dcaedc5518bc7/sandbox/websocket_execution_engine.py) مسیر اجرا از LTP و fallback وجود دارد؛ انتقال آن، جایگاه صف و اجرای سفارش ایران را ثابت نمی‌کند. BRS به همان معاملات مجازی دستی و قواعد نقد/رسید خود ادامه می‌دهد.

### 5j9/tsetmc

[InstrumentOnDate.data](https://github.com/5j9/tsetmc/blob/fdaf0e085c3ed884c828000f2f4752f91a6b5e7b/tsetmc/instruments.py#L1109) مسیر `Instrument/GetInstrumentHistory/{insCode}/{YYYYMMDD}` را معرفی می‌کند. `zTitad` و `baseVol` نامزد تعداد سهام و حجم مبنا هستند. پاسخ نمونه واقعی این نوبت، `insCode="0"` داشت و dEven نداشت؛ تاریخ و هویت عددی فقط از نشانی درخواست پشتیبانی می‌شوند. `lastDate=0` و ساعت دریافت، جای آن‌ها گذاشته نشدند.

[dps_history](https://github.com/5j9/tsetmc/blob/fdaf0e085c3ed884c828000f2f4752f91a6b5e7b/tsetmc/instruments.py#L961) منبع DPS را جدا از [price_adjustments](https://github.com/5j9/tsetmc/blob/fdaf0e085c3ed884c828000f2f4752f91a6b5e7b/tsetmc/instruments.py#L942) می‌خواند. این تفکیک برای مرحله حقوق شرکتی مفید است، اما دفتر اقتصادی هنوز نیاز به تأیید مبلغ، تاریخ استحقاق، وصول و قابلیت فروش دارد. این منبع DPS در این نوبت دریافت/تأیید نشد.

[pyproject](https://github.com/5j9/tsetmc/blob/fdaf0e085c3ed884c828000f2f4752f91a6b5e7b/pyproject.toml) همین commit پایتون>=۳٫۱۴ می‌خواهد؛ README>=۳٫۱۳ نوشته است. BRS۳٫۱۱ می‌ماند. endpoint با کد مستقل و HTTPS در provider موجود اضافه شد؛ وابستگی/GPL کپی نشد.

### tsetmc-mcp و tsetmc-api

[shaping.py](https://github.com/solitraderbusiness/tsetmc-mcp/blob/abf182f1da56169be074258e719c43969ae0cb0b/src/tsetmc_mcp/shaping.py) فیلتر/مرتب‌سازی/انتخاب ستون/سقف خروجی و شمار truncated را مشترک می‌کند؛ [safe_eval.py](https://github.com/solitraderbusiness/tsetmc-mcp/blob/abf182f1da56169be074258e719c43969ae0cb0b/src/tsetmc_mcp/filter/safe_eval.py) درخت AST محدودی را تفسیر می‌کند. الگوی ابزار فقط‌خواندنی و محدود برای AI مفید است. خطای داده که در فیلتر به False تبدیل شود در BRS باید همچنان به‌عنوان نامعلوم در مخرج کیفیت بماند.

[Snapshot](https://github.com/solitraderbusiness/tsetmc-mcp/blob/abf182f1da56169be074258e719c43969ae0cb0b/src/tsetmc_mcp/snapshot.py) staleness را از زمان به‌روزرسانی محلی snapshot می‌سنجد. استنباط از این مسیر: تازه‌بودن دریافت به‌تنهایی ساعت قیمت تک‌نماد را تأیید نمی‌کند. BRS پروتکل پیوند محتوایی/ساعت مستقل مظنه را نگه می‌دارد. سرور MCP جدید یا کلید/حساب خارجی ثبت نشد.

[DayDetails._core](https://github.com/mahs4d/tsetmc-api/blob/7b2402a2f859af4353f834e44eb0d9e1dc8cc6cd/lib/tsetmc_api/day_details/_core.py) APIهای قیمت، مظنه، دامنه و معاملات یک روز را جدا کرده است. در همین مسیر منتخب URLهای HTTP و `verify=False` دیده شد؛ BRS HTTPS و اعتبارسنجی خود را حفظ می‌کند. این مشاهده به معنی ممیزی تمام مسیرهای آن مخزن نیست.

### PyBroker

[config.py](https://github.com/edtechre/pybroker/blob/95e00fecb1006490601aca7e2e8de609f032e12d/src/pybroker/config.py) هزینه را با `fee_mode=None` و سمت معامله را با `PositionMode.DEFAULT` آغاز می‌کند. برای بورس ایران باید هزینه دو سمت، دامنه/صف، سهم صحیح، تقویم و حقوق شرکتی صریح شوند. [eval.py](https://github.com/edtechre/pybroker/blob/95e00fecb1006490601aca7e2e8de609f032e12d/src/pybroker/eval.py#L44) در مسیر `_fill_bootstrap_row` اندیس‌ها را مستقل برمی‌گزیند؛ BRS برای بازده وابسته زمانی، بازنمونه بلوکی موجود را جایگزین این پیش‌فرض نمی‌کند. [strategy.py](https://github.com/edtechre/pybroker/blob/95e00fecb1006490601aca7e2e8de609f032e12d/src/pybroker/strategy.py) و cache برای مقایسه/کارایی پژوهش آینده مرجع‌اند. نصب یا بکتست مستقل با PyBroker انجام نشد.

## آنچه پیاده شد

- `BaseDataProvider.get_instrument_history_observation` و پیاده‌سازی فقط در `data_collector/tsetmc.py`؛ هیچ دریافت‌کننده دوم TSETMC ساخته نشد.
- آرشیو پژوهشی خصوصی با برنامه، دامنه و کد محفوظ پیش از دریافت؛ حداکثر۲۰نماد،۲۰تاریخ و۱۰۰درخواست، فقط پیش از مرز holdout. پاسخ parsed JSON منبع، نشانی، زمان دریافت، شکست‌ها، هش و خروجی ارزیابی نگه داشته می‌شوند؛ بایت خام HTTP یا زمان انتشار اولیه ادعا نمی‌شود.
- کنترل هویت، تاریخ، عدد صحیح/متناهی و عدم تبدیل صفر/نامعلوم به عدد نمونه. `lastDate` تاریخ رکورد فرض نمی‌شود و اختلاف/نبود هویت و تاریخ آشکار است.
- بازبینی آفلاین تمام فایل‌ها، کد، دامنه و بازحساب ارزیابی؛ صفحه سلامت، شواهد را همراه محدودیت‌ها نشان می‌دهد.
- هیچ مایگریشن، تغییر داده عملیاتی، فیچر، روش معاملاتی، هزینه، سقف ریسک یا سفارش اضافه نشد. قابلیت تازه، دریافت/بررسی شاهد است.

راهنما و دستورها: [شواهد مشخصات تاریخی](../research/instrument-history-evidence-fa.md).

## اولویت بعد از این بررسی

۱. مشخصات تاریخی نامزد با اسناد/منابع مستقل و تاریخ مشخص تطبیق داده شود؛ پوشش کامل و اصلاحات/زمان دسترسی بررسی شوند. پس از آن، قرارداد جدا برای ورود حجم مبنا/تعداد سهام به پژوهش نوشته شود. این آرشیو خودکار مانع قدیمی DIP را رفع نمی‌کند.

۲. مبلغ و حقوق واقعی DPS/حق‌تقدم از تغییر مرجع جدا شود و روی نمونه واقعی دستی آشتی کند؛ تفاضل قیمت به سود نقدی تبدیل نشود.

۳. بسته شواهد تحلیل و خروجی AI محدود، همراه برآورد هزینه/زمان و آزمون حذف فیچر بررسی شود. ابزار MCP آینده از provider و گزارش‌های BRS استفاده کند؛ داده حساب و اجرای کد/سفارش در رابط آن قرار نگیرد.

۴. هر فرضیه آینده با نقد مشترک، هزینه، ظرفیت، کنترل مناسب و ثبت همه تلاش‌ها ارزیابی شود. داده‌ای که برای این بررسی دیده شد، دوره مستقلِ دست‌نخورده ایجاد نمی‌کند. روش پذیرفته‌شده و سودآوری همچنان تأیید نشده‌اند.

## اعتبارسنجی تغییر BRS

در این نوبت، ۱۳۱۵ آزمون آفلاین BRS موفق و چهار آزمون شبکه انتخاب‌نشده بودند؛ Ruff و هفت آزمون Node ابزار برداشت فارابی موفق شدند. ۴۰ مورد اختصاصی، تاریخ/هویت، اعداد نامعلوم، ثبت پیش از دریافت، وقفه، مهروموم، دستکاری آرشیو و رابط را بررسی کردند. CLI بازبین روی آرشیو واقعی۹نقطه موفق بود؛ رابط پرشده روی دسکتاپ/موبایل بدون خطای اجرا یا کنسول بررسی شد. CI و commit نهایی در PR همین تغییر و رسید خصوصی تحویل ثبت می‌شوند. این شواهد، اعتبار نرم‌افزار/آرشیو در دامنه گفته‌شده هستند؛ سودآوری یا تاریخچه مستقل بازار را تأیید نمی‌کنند.
