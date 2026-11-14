// Local clock and operator-armed desktop click. Never connects to a broker.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;
using Timer = System.Windows.Forms.Timer;

namespace BrsManualClock {
    public static class ClockLogic {
        public static readonly TimeZoneInfo Tehran = TimingSafety.Tehran;
        public static DateTimeOffset Now() { return TimeZoneInfo.ConvertTime(DesktopInput.PreciseUtcNow(), Tehran); }
        public static DateTimeOffset NextTarget(DateTimeOffset now) { return NextTarget(now, new TimeSpan(8, 45, 0)); }
        public static DateTimeOffset NextTarget(DateTimeOffset now, TimeSpan timeOfDay) {
            return NextTarget(now,timeOfDay,Tehran);
        }
        internal static DateTimeOffset NextTarget(DateTimeOffset now,TimeSpan timeOfDay,TimeZoneInfo zone) {
            if (timeOfDay.TotalDays >= 1 || timeOfDay.Ticks < 0) throw new ArgumentOutOfRangeException("timeOfDay");
            DateTime date = DateTime.SpecifyKind(TimeZoneInfo.ConvertTime(now, zone).Date.Add(timeOfDay), DateTimeKind.Unspecified);
            DateTimeOffset target;
            for(int day=0;day<3;day++){if(TryLocalTarget(date.AddDays(day),zone,out target)&&target>now)return target;}
            throw new ArgumentException("No unambiguous next occurrence within three days.");
        }
        internal static bool TryNonCoveringBounds(Rectangle original,Point point,Rectangle[] areas,out Rectangle result) {
            Rectangle avoid=original;avoid.Inflate(8,8);result=original;if(!avoid.Contains(point))return true;
            foreach(Rectangle area in areas){
                if(original.Width>area.Width||original.Height>area.Height)continue;
                foreach(Point corner in new Point[]{new Point(area.Left,area.Top),new Point(area.Right-original.Width,area.Top),new Point(area.Left,area.Bottom-original.Height),new Point(area.Right-original.Width,area.Bottom-original.Height)}){
                    Rectangle candidate=new Rectangle(corner,original.Size),margin=candidate;margin.Inflate(8,8);
                    if(!margin.Contains(point)){result=candidate;return true;}
                }
            }
            return false;
        }
        public static bool TryLocalTarget(DateTime local,TimeZoneInfo zone,out DateTimeOffset target) {
            target=default(DateTimeOffset); local=DateTime.SpecifyKind(local,DateTimeKind.Unspecified);
            if(zone==null || zone.IsInvalidTime(local) || zone.IsAmbiguousTime(local))return false;
            target=new DateTimeOffset(local,zone.GetUtcOffset(local));return true;
        }
        public static string NormalizeDigits(string value) {
            if (value == null) return "";
            StringBuilder output = new StringBuilder();
            foreach (char c in value.Trim()) {
                if (c >= '\u06f0' && c <= '\u06f9') output.Append((char)('0' + c - '\u06f0'));
                else if (c >= '\u0660' && c <= '\u0669') output.Append((char)('0' + c - '\u0660'));
                else output.Append(c == '\u2212' ? '-' : c);
            }
            return output.ToString();
        }
        public static bool TryTime(string text, out TimeSpan time) { return TimeSpan.TryParseExact(NormalizeDigits(text), @"hh\:mm\:ss\.fff", CultureInfo.InvariantCulture, out time) && time.Ticks >= 0 && time.TotalDays < 1; }
        public static string Jalali(DateTimeOffset value) { PersianCalendar c = new PersianCalendar(); DateTime date = TimeZoneInfo.ConvertTime(value, Tehran).DateTime; return String.Format(CultureInfo.InvariantCulture, "{0:0000}/{1:00}/{2:00}", c.GetYear(date), c.GetMonth(date), c.GetDayOfMonth(date)); }
        public static bool TryTarget(string dateText, string timeText, out DateTimeOffset target) {
            target = default(DateTimeOffset); TimeSpan time; string date = NormalizeDigits(dateText);
            if (!TryTime(timeText, out time) || date.Length != 10 || date[4] != '/' || date[7] != '/') return false;
            int year, month, day;
            if (!Int32.TryParse(date.Substring(0,4), NumberStyles.None, CultureInfo.InvariantCulture, out year) || !Int32.TryParse(date.Substring(5,2), NumberStyles.None, CultureInfo.InvariantCulture, out month) || !Int32.TryParse(date.Substring(8,2), NumberStyles.None, CultureInfo.InvariantCulture, out day)) return false;
            try { DateTime local = new PersianCalendar().ToDateTime(year, month, day, 0, 0, 0, 0).Add(time); return TryLocalTarget(local,Tehran,out target); }
            catch (ArgumentException) { return false; }
        }
        public static string Decide(DateTimeOffset now, DateTimeOffset target, bool armed, double wallDeltaMs, double monoDeltaMs) {
            if (!armed) return "idle";
            if (!TimingSafety.Finite(wallDeltaMs) || !TimingSafety.Finite(monoDeltaMs) || monoDeltaMs < 0 || Math.Abs(wallDeltaMs - monoDeltaMs) > 100) return "clock_change";
            if (monoDeltaMs > 250) return "scheduler_gap";
            return now >= target ? "cue" : "wait";
        }
    }

    public sealed class ClockWindow : Form {
        private const int EscapeId = 0x5ad1;
        private readonly TimingJournal journal;
        private readonly string workspaceRoot;
        private readonly Timer timer = new Timer();
        private readonly Stopwatch stopwatch = Stopwatch.StartNew();
        private readonly Label clock, targetLabel, countdown, status;
        private readonly CheckBox sound;
        private CheckBox rehearsal;
        private TextBox clickTime, clickDate, pointX, pointY;
        private NumericUpDown maximumLate;
        private Button pickButton, clickButton, nextButton;
        private Label clickPreview, pointPreview;
        private ClickSchedule clickSchedule;
        private TargetSnapshot selectedTarget;
        private bool armed, picking, suppressCoordinates, escapeReserved, initialized, failed;
        private bool startupConfirmed=true;
        private string startupReceipt, startupNonce;
        private readonly bool smokeTest;
        private DateTimeOffset target, lastWall, pickDeadline, previewUpdated;
        private double lastMono;
        private readonly UiHealthState uiHealth=new UiHealthState();
        private readonly List<Font> ownedFonts=new List<Font>();
        private readonly object resultRecordGate=new object();
        private string recordedResult;
        private string attemptedResult;
        private bool resultRecordUnresolved;
        private int resourcesClosed,fontsDisposed;
        private bool pickerRestorePending;
        private Rectangle pickerRestoreBounds;
        private FormWindowState pickerRestoreState;
        private long selectionLifecycle;
        private string handledResult;
        public bool CueFired { get; private set; }
        public event Action Ready;
        public event Action<Exception> StartupFailed;
        public void RequireStartupConfirmation(string receipt,string nonce) { startupReceipt=receipt;startupNonce=nonce;startupConfirmed=false;SetEditing(false);SetStatus("در انتظار تأیید راه‌انداز — زمان‌بندی غیرفعال است",Color.DarkBlue); }
        public ClockWindow(string workspace, bool runSmokeTest) {
            workspaceRoot=Path.GetFullPath(workspace);
            smokeTest = runSmokeTest;
            journal = new TimingJournal(workspace);
            Text = "Timing Clock " + TimingSafety.Version + " | ساعت و زمان‌بندی کلیک";
            AutoScaleDimensions=new SizeF(96,96); AutoScaleMode=AutoScaleMode.Dpi;
            BackColor = Color.FromArgb(245, 248, 252); Font = OwnFont("Tahoma", 10,FontStyle.Regular); RightToLeft = RightToLeft.Yes;
            StartPosition = FormStartPosition.CenterScreen; TopMost = true; AutoScroll = true;
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            ClientSize = new Size(Math.Min(720, Math.Max(500, area.Width - 60)), Math.Min(720, Math.Max(380, area.Height - 70)));
            MinimumSize = new Size(Math.Min(580, area.Width), Math.Min(400, area.Height));
            AutoScrollMinSize = new Size(690, 716); KeyPreview = true;
            AddLabel("ساعت تهران از ویندوز — دقت UTC و ساعت هسته تأیید نشده است", 10, 28, 10);
            clock = AddLabel("", 42, 70, 40); clock.Font = OwnFont("Consolas", 40, FontStyle.Bold); clock.RightToLeft = RightToLeft.No; clock.ForeColor = Color.FromArgb(20,55,105);
            targetLabel = AddLabel("", 112, 28, 11);
            countdown = AddLabel("", 143, 42, 21); countdown.RightToLeft = RightToLeft.No;
            status = AddLabel("غیرفعال — هیچ کلیکی برنامه‌ریزی نشده است", 191, 43, 10); status.AccessibleName = "وضعیت زمان‌بندی";
            AddButton("هشدار ۰۸:۴۵", 462, 243, 202).Click += delegate { ArmCue(ClockLogic.NextTarget(ClockLogic.Now())); };
            AddButton("آزمون هشدار ۵ ثانیه", 244, 243, 202).Click += delegate { ArmCue(ClockLogic.Now().AddSeconds(5)); };
            AddButton("لغو همه", 26, 243, 202).Click += delegate { CancelAll("cancelled"); };
            sound = new CheckBox { Text = "صدای هشدار", Checked = true }; sound.SetBounds(510,290,150,27); Controls.Add(sound);
            CheckBox onTop = new CheckBox { Text = "همیشه روی پنجره‌ها", Checked = true }; onTop.SetBounds(282,290,200,27); onTop.CheckedChanged += delegate { TopMost = onTop.Checked; }; Controls.Add(onTop);
            AddLabel("پس از فعال‌سازی، در ثانیهٔ آخر از موس و کیبورد استفاده نکنید.", 326, 38, 10);
            AddClickControls();
            target = ClockLogic.NextTarget(ClockLogic.Now()); lastWall = ClockLogic.Now(); lastMono = stopwatch.Elapsed.TotalMilliseconds;
            timer.Interval = 16; timer.Tick += delegate { SafeTick(); };
            KeyDown += delegate(object sender, KeyEventArgs e) { if (e.KeyCode == Keys.Escape) { CancelAll("escape"); e.Handled = true; } };
            SystemEvents.SessionSwitch += SessionChanged;
            SystemEvents.PowerModeChanged += PowerChanged;
            SystemEvents.DisplaySettingsChanged += DisplaysChanged;
            SystemEvents.TimeChanged += TimeChanged;
            FormClosing += delegate { uiHealth.InvalidateWithReason("closed");SignalCancellation("closed"); };
            FormClosed += HandleClosed;
            journal.Append("started", new { version=TimingSafety.Version, timezone="Asia/Tehran", armed=false, broker_api=false, utc_accuracy_verified=false });
            initialized = true;
            Shown += delegate {
                try { lastWall = ClockLogic.Now(); lastMono = stopwatch.Elapsed.TotalMilliseconds; uiHealth.Pulse(); journal.Append("window_shown", new { armed=false }); timer.Start(); SafeTick(); if(failed)throw new InvalidOperationException("Initial UI check failed."); if (Ready != null) Ready(); if (smokeTest && startupConfirmed) ArmCue(ClockLogic.Now().AddSeconds(5)); }
                catch (Exception ex) { FailClosed(ex); if(StartupFailed!=null)StartupFailed(ex); BeginInvoke(new Action(Close)); }
            };
        }
        private Font OwnFont(string family,float size,FontStyle style) { Font font=new Font(family,size,style);ownedFonts.Add(font);return font; }
        private Label AddLabel(string text, int y, int height, float size) { Label label = new Label { Text=text, Font=OwnFont("Tahoma",size,FontStyle.Regular), TextAlign=ContentAlignment.MiddleCenter }; label.SetBounds(16,y,658,height); Controls.Add(label); return label; }
        private Button AddButton(string text, int x, int y, int width) { Button button = new Button { Text=text }; button.SetBounds(x,y,width,37); Controls.Add(button); return button; }
        private void AddClickControls() {
            GroupBox box = new GroupBox { Text="یک کلیک با تاریخ و زمان مشخص" }; box.SetBounds(16,372,658,328); Controls.Add(box);
            Label timeCaption = new Label { Text="زمان اجرا", TextAlign=ContentAlignment.MiddleRight }; timeCaption.SetBounds(535,28,95,28); box.Controls.Add(timeCaption);
            clickTime = new TextBox { Text="08:45:00.000", RightToLeft=RightToLeft.No, Font=OwnFont("Consolas",17,FontStyle.Regular), AccessibleName="زمان اجرا HH:mm:ss.fff" }; clickTime.SetBounds(315,27,212,35); box.Controls.Add(clickTime);
            Label tolerance = new Label { Text="حد تأخیر (ms)", TextAlign=ContentAlignment.MiddleRight }; tolerance.SetBounds(102,28,190,28); box.Controls.Add(tolerance);
            maximumLate = new NumericUpDown { Minimum=1, Maximum=5000, Value=100, RightToLeft=RightToLeft.No, AccessibleName="حد تأخیر میلی‌ثانیه" }; maximumLate.SetBounds(25,30,75,28); box.Controls.Add(maximumLate);
            pointX = new TextBox { RightToLeft=RightToLeft.No, AccessibleName="مختصات X" }; pointX.SetBounds(470,76,70,27); box.Controls.Add(pointX);
            Label x = new Label { Text="X", RightToLeft=RightToLeft.No }; x.SetBounds(547,80,25,24); box.Controls.Add(x);
            pointY = new TextBox { RightToLeft=RightToLeft.No, AccessibleName="مختصات Y" }; pointY.SetBounds(355,76,70,27); box.Controls.Add(pointY);
            Label y = new Label { Text="Y", RightToLeft=RightToLeft.No }; y.SetBounds(433,80,25,24); box.Controls.Add(y);
            pickButton = new Button { Text="انتخاب مکان موس (۳ ثانیه)" }; pickButton.SetBounds(25,73,270,34); box.Controls.Add(pickButton);
            pickButton.Click += delegate {
                if (clickSchedule != null || failed) return;
                try {
                    ReserveEscape(); armed=false; picking=true; selectedTarget=null;
                    suppressCoordinates=true;try{pointX.Clear();pointY.Clear();}finally{suppressCoordinates=false;}
                    pointPreview.Text="انتخاب مقصد در حال انجام است";SetEditing(false);
                    lastWall=ClockLogic.Now();lastMono=stopwatch.Elapsed.TotalMilliseconds;
                    pickDeadline=lastWall.AddSeconds(3);SetStatus("تا ۳ ثانیه موس را روی مقصد ببرید؛ کلیکی انجام نمی‌شود",Color.DarkBlue);
                    pickerRestoreBounds=WindowState==FormWindowState.Normal?Bounds:RestoreBounds;pickerRestoreState=WindowState;pickerRestorePending=true;WindowState=FormWindowState.Minimized;
                }
                catch (Exception ex) { picking=false;RestorePicker(false,new DesktopInput.Point());ReleaseEscape();SetEditing(true);SetStatus(ex.Message,Color.Firebrick); }
            };
            Label dateCaption = new Label { Text="تاریخ شمسی", TextAlign=ContentAlignment.MiddleRight }; dateCaption.SetBounds(535,115,95,28); box.Controls.Add(dateCaption);
            clickDate = new TextBox { Text=ClockLogic.Jalali(ClockLogic.NextTarget(ClockLogic.Now())), RightToLeft=RightToLeft.No, AccessibleName="تاریخ اجرا YYYY/MM/DD" }; clickDate.SetBounds(315,115,212,28); box.Controls.Add(clickDate);
            nextButton = new Button { Text="انتخاب تاریخ وقوع بعدی" }; nextButton.SetBounds(25,111,270,34); box.Controls.Add(nextButton);
            nextButton.Click += delegate { TimeSpan time; if (ClockLogic.TryTime(clickTime.Text,out time)) clickDate.Text=ClockLogic.Jalali(ClockLogic.NextTarget(ClockLogic.Now(),time)); };
            clickPreview = new Label { TextAlign=ContentAlignment.MiddleCenter }; clickPreview.SetBounds(25,151,605,25); box.Controls.Add(clickPreview);
            pointPreview = new Label { Text="مقصد انتخاب نشده است", TextAlign=ContentAlignment.MiddleCenter }; pointPreview.SetBounds(25,180,605,25); box.Controls.Add(pointPreview);
            clickButton = new Button { Text="فعال‌کردن یک کلیک" }; clickButton.SetBounds(350,213,280,37); box.Controls.Add(clickButton); clickButton.Click += delegate { StartConfiguredClick(); };
            Button cancel = new Button { Text="لغو زمان‌بندی (Esc)" }; cancel.SetBounds(25,213,305,37); box.Controls.Add(cancel); cancel.Click += delegate { CancelAll("cancelled"); };
            rehearsal=new CheckBox {Text="تمرین بدون حرکت یا کلیک موس (فقط زمان‌بند و کنترل‌ها)",AccessibleName="تمرین بدون کلیک"};rehearsal.SetBounds(25,257,605,29);box.Controls.Add(rehearsal);
            rehearsal.CheckedChanged+=delegate{clickButton.Text=rehearsal.Checked?"فعال‌کردن تمرین بدون کلیک":"فعال‌کردن یک کلیک";};
            Label help = new Label { Text="حداکثر ۲۴ ساعت؛ رایانه و نمایشگر روشن می‌مانند؛ هر فعال‌سازی فقط یک بار.", TextAlign=ContentAlignment.MiddleCenter }; help.SetBounds(25,289,605,29); box.Controls.Add(help);
            clickTime.TextChanged += delegate { UpdatePreview(); }; clickDate.TextChanged += delegate { UpdatePreview(); };
            pointX.TextChanged += delegate { if (!suppressCoordinates && clickSchedule==null) CapturePoint(); }; pointY.TextChanged += delegate { if (!suppressCoordinates && clickSchedule==null) CapturePoint(); };
            UpdatePreview();
        }
        private void SetStatus(string text, Color color) { status.Text=text; status.AccessibleName="وضعیت: "+text; status.ForeColor=color; }
        private bool RestorePicker(bool preservePoint,DesktopInput.Point point) {
            if(!pickerRestorePending)return true;pickerRestorePending=false;if(IsDisposed||Disposing)return false;
            Rectangle bounds=pickerRestoreBounds;bool placed=true;
            if(preservePoint){List<Rectangle> areas=new List<Rectangle>();foreach(Screen screen in Screen.AllScreens)areas.Add(screen.WorkingArea);placed=ClockLogic.TryNonCoveringBounds(bounds,new Point(point.X,point.Y),areas.ToArray(),out bounds);}
            WindowState=FormWindowState.Normal;Bounds=bounds;
            if(!preservePoint||!placed)WindowState=pickerRestoreState;
            return placed;
        }
        private void ReserveEscape() { if (!escapeReserved) { if (!TargetGuard.RegisterHotKey(Handle,EscapeId,0x4000,27)) throw new InvalidOperationException("میانبر Escape در دسترس نیست؛ زمان‌بندی فعال نشد"); escapeReserved=true; } }
        private void ReleaseEscape() { if (escapeReserved) { TargetGuard.UnregisterHotKey(Handle,EscapeId); escapeReserved=false; } }
        protected override void WndProc(ref Message m) {
            if (initialized && m.Msg==0x0312 && m.WParam.ToInt32()==EscapeId) { CancelAll("escape"); return; }
            if (initialized && (m.Msg==0x007e || m.Msg==0x02e0)) LifecycleChanged("desktop_changed");
            base.WndProc(ref m);
        }
        private void SessionChanged(object sender, SessionSwitchEventArgs e) { LifecycleChanged("session_changed"); }
        private void PowerChanged(object sender, PowerModeChangedEventArgs e) { if (e.Mode!=PowerModes.StatusChange) LifecycleChanged("power_changed"); }
        private void DisplaysChanged(object sender, EventArgs e) { LifecycleChanged("desktop_changed"); }
        private void TimeChanged(object sender, EventArgs e) { LifecycleChanged("clock_change"); }
        private void SignalCancellation(string reason) { ClickSchedule current=Volatile.Read(ref clickSchedule); if (current!=null) current.Cancel(reason); }
        private void LifecycleChanged(string reason) {
            uiHealth.InvalidateWithReason(reason);SignalCancellation(reason);
            long invalidated=uiHealth.Generation;
            if (!IsDisposed && IsHandleCreated) try { BeginInvoke(new Action(delegate { if(uiHealth.Generation!=invalidated||selectionLifecycle>=invalidated)return;ClearSelection();CancelAll(reason); })); } catch (InvalidOperationException) { }
        }
        private void UpdatePreview() {
            previewUpdated=ClockLogic.Now(); DateTimeOffset when;
            if (!ClockLogic.TryTarget(clickDate.Text,clickTime.Text,out when)) { clickPreview.Text="تاریخ یا زمان نامعتبر؛ نمونه: 1405/08/03 08:45:00.125"; clickPreview.ForeColor=Color.Firebrick; return; }
            bool future=when>ClockLogic.Now(); clickPreview.ForeColor=future?Color.DarkBlue:Color.Firebrick;
            clickPreview.Text=(future?"زمان انتخابی: ":"این زمان گذشته است: ")+ClockLogic.Jalali(when)+"  "+when.ToString("HH:mm:ss.fff",CultureInfo.InvariantCulture);
        }
        private void CapturePoint() {
            selectedTarget=null; int x,y;
            if (!Int32.TryParse(ClockLogic.NormalizeDigits(pointX.Text),out x) || !Int32.TryParse(ClockLogic.NormalizeDigits(pointY.Text),out y)) { pointPreview.Text="مختصات X و Y کامل نیست"; return; }
            try {
                long generation=uiHealth.Generation;
                TargetSnapshot capture=TargetGuard.Capture(new DesktopInput.Point { X=x,Y=y },Handle);
                if(generation!=uiHealth.Generation)throw new TargetGuardException("session_changed");
                selectedTarget=capture;selectionLifecycle=generation;pointPreview.Text="مقصد: "+selectedTarget.ProcessName+" — X="+x+", Y="+y; pointPreview.ForeColor=Color.DarkBlue;
            }
            catch (Exception ex) { pointPreview.Text=Reason(ex is TargetGuardException?((TargetGuardException)ex).Reason:"target_unavailable"); pointPreview.ForeColor=Color.Firebrick; }
        }
        private void ClearSelection() {
            selectedTarget=null;suppressCoordinates=true;
            try{pointX.Clear();pointY.Clear();}finally{suppressCoordinates=false;}
            pointPreview.Text="زمینه تغییر کرده است؛ مقصد را دوباره انتخاب کنید";pointPreview.ForeColor=Color.Firebrick;
        }
        private void SetEditing(bool enabled) { clickDate.Enabled=clickTime.Enabled=pointX.Enabled=pointY.Enabled=maximumLate.Enabled=pickButton.Enabled=clickButton.Enabled=nextButton.Enabled=rehearsal.Enabled=enabled && !failed && startupConfirmed && !picking; }
        private void CancelAll(string reason) {
            ClickSchedule current=clickSchedule;
            if(current!=null&&current.CompletedResult!=null){ConsumeResult(current.CompletedResult);return;}
            armed=false; picking=false;RestorePicker(false,new DesktopInput.Point()); bool observing=clickSchedule!=null&&clickSchedule.Phase=="rehearsal_observing";bool stopped=clickSchedule==null || clickSchedule.Cancel(reason); ReleaseEscape();
            if(clickSchedule==null)SetEditing(true);
            SetStatus(observing?"تمرین به مرحلهٔ مشاهده رسیده است؛ هیچ ورودی موس ارسال نمی‌شود":stopped?Reason(reason):"ارسال قطعی شده است؛ لغو آن قابل تضمین نیست. نتیجهٔ مقصد را بررسی کنید",Color.DimGray);
            try { journal.Append("cancel_requested",new { reason=reason, cancellation_accepted=stopped }); } catch (Exception) { SetStatus(status.Text+" — لاگ ذخیره نشد",Color.Firebrick); }
        }
        private void StartConfiguredClick() {
            if (clickSchedule!=null || failed || !startupConfirmed || picking) return;
            ClickSchedule prepared=null;
            try {
                DateTimeOffset when;
                if (!ClockLogic.TryTarget(clickDate.Text,clickTime.Text,out when)) throw new ArgumentException("تاریخ و زمان را کامل و با سه رقم میلی‌ثانیه وارد کنید");
                if (selectedTarget==null) throw new ArgumentException("ابتدا مقصد را انتخاب یا مختصات را دوباره وارد کنید");
                if(selectionLifecycle!=uiHealth.Generation){ClearSelection();throw new TargetGuardException("session_changed");}
                string changed=TargetGuard.Validate(selectedTarget); if (changed!=null) throw new TargetGuardException(changed);
                ReserveEscape(); long generation=selectionLifecycle;
                ClickPlan plan=new ClickPlan(when,selectedTarget,(double)maximumLate.Value,null,rehearsal.Checked,uiHealth,generation);
                prepared=ClickSchedule.Prepare(plan,CompleteClick);
                clickSchedule=prepared; armed=false; picking=false; target=when; handledResult=null; SetEditing(false);
                try { journal.Append("click_armed",new { schedule_id=prepared.Id,mode=plan.Rehearsal?"rehearsal":"click",target_tehran=TimingSafety.Iso(when),x=plan.Point.X,y=plan.Point.Y,maximum_lateness_ms=plan.MaximumLatenessMs,minimum_lead_ms=plan.MinimumLeadMs,maximum_horizon_hours=24,power_policy="system_and_display_awake_while_armed",quiet_period_ms=plan.Snapshot.QuietMilliseconds,root=plan.RootWindow.ToInt64(),target_process_id=plan.Snapshot.ProcessId,root_process_id=plan.Snapshot.RootProcessId }); }
                catch(Exception error){FailClosed(error);throw;}
                uiHealth.Pulse();
                prepared.Start();
                SetStatus(plan.Rehearsal?"تمرین فعال است؛ هیچ ورودی موس ارسال نمی‌شود — Esc برای لغو":"یک کلیک فعال است؛ Esc برای لغو — در ثانیهٔ آخر ورودی ندهید",Color.DarkBlue);
            } catch (Exception ex) {
                if (prepared!=null) prepared.Dispose(); clickSchedule=null; ReleaseEscape(); SetEditing(true);
                string message=ex is TargetGuardException?Reason(((TargetGuardException)ex).Reason):ex.Message;
                if (ex is ScheduleTimingException) message=String.Format(CultureInfo.InvariantCulture,"زمان باید دست‌کم {0} میلی‌ثانیه در آینده باشد؛ تاریخ را هم بررسی کنید",((ScheduleTimingException)ex).RequiredLeadMs);
                if(ex is ScheduleHorizonException)message="زمان کلیک باید در ۲۴ ساعت آینده باشد؛ تاریخ را بررسی کنید";
                SetStatus(failed?"ثبت شواهد ناموفق بود؛ زمان‌بندی متوقف شد. پس از رفع خطا برنامه را دوباره باز کنید":message,Color.Firebrick);
                try { TargetGuardException guard=ex as TargetGuardException;journal.Append("click_arm_failed",new { error_type=ex.GetType().Name,reason=guard==null?null:guard.Reason,native_code=guard==null?null:guard.NativeCode }); } catch (Exception) { }
            }
        }
        private void CompleteClick(ClickResult result) { if (!IsDisposed && IsHandleCreated) try { BeginInvoke(new Action(delegate { ConsumeResult(result); })); } catch (InvalidOperationException) { } }
        private bool RecordResultOnce(ClickResult result,ClickSchedule owner) {
            lock(resultRecordGate){
                if(recordedResult==result.ScheduleId)return true;
                if(attemptedResult==result.ScheduleId)return false;
                attemptedResult=result.ScheduleId;resultRecordUnresolved=true;
                for(int attempt=0;attempt<2;attempt++)try{
                    journal.Append("click_result",new {
                        schedule_id=result.ScheduleId,mode=result.Rehearsal?"rehearsal":"click",outcome=result.Outcome,target_tehran=TimingSafety.Iso(result.Target),
                        last_timing_check_tehran=result.LastTimingCheckAt.HasValue?TimingSafety.Iso(result.LastTimingCheckAt.Value):null,
                        dispatch_check_tehran=result.DispatchCheckAt.HasValue?TimingSafety.Iso(result.DispatchCheckAt.Value):null,
                        deadline_gate_lateness_ms=result.LatenessMs,lateness_ms=result.LatenessMs,lateness_basis="deadline_gate_sample",commit_accepted=result.CommitAccepted,commit_basis="cancellation_cas_won_not_native_invocation",native_call_invoked=result.NativeCallInvoked,pre_commit_waits=result.PreCommitWaits,
                        observed_tehran=result.ObservedAt.HasValue?TimingSafety.Iso(result.ObservedAt.Value):null,
                        rehearsal_observed_tehran=result.RehearsalObservedAt.HasValue?TimingSafety.Iso(result.RehearsalObservedAt.Value):null,
                        queue_call_started_tehran=result.QueueCallStarted.HasValue?TimingSafety.Iso(result.QueueCallStarted.Value):null,
                        queue_call_returned_tehran=result.QueueCallReturned.HasValue?TimingSafety.Iso(result.QueueCallReturned.Value):null,
                        pre_native_check_tehran=result.PreNativeCheckAt.HasValue?TimingSafety.Iso(result.PreNativeCheckAt.Value):null,
                        native_call_entry_sample_tehran=result.NativeCallEntrySample.HasValue?TimingSafety.Iso(result.NativeCallEntrySample.Value):null,
                        native_call_return_sample_tehran=result.NativeCallReturnSample.HasValue?TimingSafety.Iso(result.NativeCallReturnSample.Value):null,
                        native_call_duration_ticks=result.NativeCallDurationTicks,native_stopwatch_frequency=Stopwatch.Frequency,
                        managed_preinvoke_sample_tehran=result.NativeCallEntrySample.HasValue?TimingSafety.Iso(result.NativeCallEntrySample.Value):null,
                        managed_postinvoke_sample_tehran=result.NativeCallReturnSample.HasValue?TimingSafety.Iso(result.NativeCallReturnSample.Value):null,native_sample_basis="managed_adjacent_samples_not_kernel_entry_or_target_acceptance",
                        native_safety_duration_ms=result.NativeSafetyDurationMs,commit_to_preinvoke_sample_ms=result.CommitToNativeSampleMs,
                        native_call_duration_ms=result.NativeCallDurationTicks.HasValue?(double?)(result.NativeCallDurationTicks.Value*1000.0/Stopwatch.Frequency):null,
                        native_call_lateness_ms=result.NativeCallLatenessMs,send_timing_basis=result.SendTimingBasis,
                        send_started_tehran=result.SendStarted.HasValue?TimingSafety.Iso(result.SendStarted.Value):null,
                        send_returned_tehran=result.SendReturned.HasValue?TimingSafety.Iso(result.SendReturned.Value):null,
                        release_entry_sample_tehran=result.ReleaseEntrySample.HasValue?TimingSafety.Iso(result.ReleaseEntrySample.Value):null,
                        release_return_sample_tehran=result.ReleaseReturnSample.HasValue?TimingSafety.Iso(result.ReleaseReturnSample.Value):null,release_duration_ticks=result.ReleaseDurationTicks,
                        dispatch_finished_tehran=result.DispatchFinishedAt.HasValue?TimingSafety.Iso(result.DispatchFinishedAt.Value):null,guard_validation_duration_ms=result.GuardValidationDurationMs,
                        queued_events=result.QueuedEvents,release_events=result.ReleaseEvents,native_error=result.NativeError,high_resolution_timer=result.HighResolutionTimer,
                        dispatch_attempted=result.DispatchAttempted,timing_error=result.TimingError,cleanup_error=result.CleanupError,
                        notification_error=owner==null?null:owner.NotificationError,notification_pending=owner!=null&&owner.CompletionCallbackPending,completion_callback_pending_at_record=owner!=null&&owner.CompletionCallbackPending,completion_callback_error_at_record=owner==null?null:owner.NotificationError,target_processing_time_verified=false
                    });
                    recordedResult=result.ScheduleId;resultRecordUnresolved=false;return true;
                }catch(JournalPrewriteException error){if(attempt==1){journal.TryFailureMarker("click_result_prewrite",error);return false;}}
                catch(Exception error){journal.TryFailureMarker("click_result",error);return false;}
                return false;
            }
        }
        private void ConsumeResult(ClickResult result) {
            if (clickSchedule==null || result.ScheduleId!=clickSchedule.Id || handledResult==result.ScheduleId) return;
            handledResult=result.ScheduleId; bool logFailed=!RecordResultOnce(result,clickSchedule);
            ClickSchedule finished=clickSchedule; clickSchedule=null; finished.Dispose(); ReleaseEscape();
            bool fault=result.DispatchAttempted && result.Outcome!="queued" || result.TimingError!=null || result.CleanupError!=null || logFailed;
            if (fault) failed=true;
            if(result.Outcome!="queued" && result.Outcome!="rehearsal_due" && result.Outcome!="cancelled" && result.Outcome!="escape")ClearSelection();
            SetEditing(true);
            string message=result.Outcome=="queued"?String.Format(CultureInfo.InvariantCulture,"ویندوز ورودی را پذیرفت؛ نمونهٔ کنترل زمان {0:F3} ms پس از هدف — زمان واکنش مقصد نامعلوم",result.LatenessMs):Reason(result.Outcome);
            if(result.Outcome=="rehearsal_due")message=String.Format(CultureInfo.InvariantCulture,"تمرین تمام شد؛ مشاهدهٔ زمان‌بند {0:F3} ms پس از هدف — هیچ ورودی موس ارسال نشد",result.LatenessMs);
            if(result.Outcome=="too_late" && result.LatenessMs.HasValue)message+=String.Format(CultureInfo.InvariantCulture," (تأخیر اندازه‌گیری‌شده: {0:F3} ms)",result.LatenessMs.Value);
            if (result.TimingError!=null || result.CleanupError!=null) message+=" — شواهد زمان/پاک‌سازی کامل نیست";
            if (logFailed) message+=" — ذخیرهٔ لاگ ناموفق بود";
            if (fault) message+=" — پس از بررسی مقصد، برنامه را دوباره باز کنید";
            SetStatus(message,result.Outcome=="rehearsal_due" && !fault?Color.DarkBlue:result.Outcome=="queued" && !fault?Color.DarkGreen:Color.Firebrick);
        }
        private void ArmCue(DateTimeOffset when) {
            if (failed || !startupConfirmed || clickSchedule!=null) { SetStatus("برنامه آماده نیست یا زمان‌بندی کلیک فعال است",Color.Firebrick); return; }
            picking=false;RestorePicker(false,new DesktopInput.Point()); ReleaseEscape();SetEditing(true); target=when; lastWall=ClockLogic.Now(); lastMono=stopwatch.Elapsed.TotalMilliseconds;
            try { journal.Append("armed",new { target_tehran=TimingSafety.Iso(when),mode="cue_only" }); armed=true; SetStatus("هشدار فعال؛ فقط رنگ و صدای محلی تغییر می‌کند",Color.DarkBlue); }
            catch (Exception ex) { FailClosed(ex); }
        }
        private void SafeTick() {
            try { TickClock(); } catch (Exception ex) { FailClosed(ex); }
        }
        private void TickClock() {
            timer.Interval=WindowState==FormWindowState.Minimized?100:16;
            uiHealth.Pulse(); DateTimeOffset now=ClockLogic.Now(); double mono=stopwatch.Elapsed.TotalMilliseconds;
            if (!startupConfirmed && startupReceipt!=null) {
                TimingSafety.RequireSafePath(startupReceipt+".abort",workspaceRoot);
                TimingSafety.RequireSafePath(startupReceipt+".confirm",workspaceRoot);
                if (File.Exists(startupReceipt+".abort")) throw new TimeoutException("Launcher aborted startup.");
                if (File.Exists(startupReceipt+".confirm")) {
                    Dictionary<string,object> confirmation=null;
                    try { confirmation=new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(startupReceipt+".confirm")); } catch (IOException) { } catch (ArgumentException) { }
                    object value;
                    if (confirmation!=null && confirmation.TryGetValue("nonce",out value) && Convert.ToString(value,CultureInfo.InvariantCulture)==startupNonce) { journal.Append("startup_confirmed",new {armed=false});startupConfirmed=true;SetEditing(true);SetStatus("آماده — هیچ کلیکی برنامه‌ریزی نشده است",Color.DarkBlue);if(smokeTest)ArmCue(now.AddSeconds(5)); }
                }
            }
            if (clickSchedule!=null && clickSchedule.CompletedResult!=null) ConsumeResult(clickSchedule.CompletedResult);
            if (clickSchedule==null && (now-previewUpdated).TotalSeconds>=1) UpdatePreview();
            string continuity=ClockLogic.Decide(now,target,true,(now-lastWall).TotalMilliseconds,mono-lastMono);
            if (picking && (continuity=="clock_change" || continuity=="scheduler_gap" || (now-pickDeadline).TotalMilliseconds>250)) { picking=false;RestorePicker(false,new DesktopInput.Point()); ReleaseEscape();SetEditing(true); SetStatus("انتخاب نقطه به علت وقفه لغو شد؛ دوباره انتخاب کنید",Color.Firebrick); }
            if (picking && now>=pickDeadline) {
                picking=false; ReleaseEscape(); DesktopInput.Point point=DesktopInput.CursorPoint(); suppressCoordinates=true;
                bool placement=RestorePicker(true,point);
                try { pointX.Text=point.X.ToString(CultureInfo.InvariantCulture); pointY.Text=point.Y.ToString(CultureInfo.InvariantCulture); } finally { suppressCoordinates=false; }
                if(placement)CapturePoint();else{selectedTarget=null;pointPreview.Text="فضای کافی برای نمایش ساعت بدون پوشاندن مقصد نیست؛ جای پنجره یا مقصد را تغییر دهید";}
                SetStatus(selectedTarget==null?"مقصد معتبر نیست؛ دوباره انتخاب کنید":"مقصد انتخاب شد؛ تاریخ و زمان را بررسی کنید",selectedTarget==null?Color.Firebrick:Color.DarkBlue);
                SetEditing(true);
            }
            clock.Text=now.ToString("HH:mm:ss.fff",CultureInfo.InvariantCulture);
            targetLabel.Text="هدف: "+ClockLogic.Jalali(target)+"  "+target.ToString("HH:mm:ss.fff",CultureInfo.InvariantCulture);
            TimeSpan left=target>now?target-now:TimeSpan.Zero;
            countdown.Text=left.TotalDays>=1?String.Format(CultureInfo.InvariantCulture,"{0}d {1}",(int)left.TotalDays,left.ToString(@"hh\:mm\:ss\.fff")):left.ToString(@"hh\:mm\:ss\.fff");
            string decision=ClockLogic.Decide(now,target,armed,(now-lastWall).TotalMilliseconds,mono-lastMono);
            if (decision=="clock_change" || decision=="scheduler_gap") { armed=false; journal.Append("invalidated",new { reason=decision }); SetStatus(Reason(decision),Color.Firebrick); if (smokeTest) BeginInvoke(new Action(Close)); }
            else if (decision=="cue") {
                armed=false; CueFired=true; journal.Append("cue",new { target_tehran=TimingSafety.Iso(target),observed_lateness_ms=(now-target).TotalMilliseconds,sound_requested=sound.Checked }); SetStatus("زمان هشدار رسید — هشدار محلی",Color.DarkGreen);
                if (sound.Checked) System.Media.SystemSounds.Exclamation.Play(); if (smokeTest) BeginInvoke(new Action(Close));
            }
            lastWall=now; lastMono=mono; uiHealth.Pulse();
        }
        public void FailClosed(Exception exception) {
            uiHealth.FailWithReason("ui_error");failed=true; armed=false; picking=false;
            List<string> errors=new List<string>();
            CleanupStep(errors,"cancel",delegate{SignalCancellation("ui_error");});
            CleanupStep(errors,"timer",timer.Stop);
            CleanupStep(errors,"picker",delegate{RestorePicker(false,new DesktopInput.Point());});
            CleanupStep(errors,"escape",ReleaseEscape);
            CleanupStep(errors,"editing",delegate{SetEditing(false);});
            CleanupStep(errors,"status",delegate{SetStatus("برنامه به خطا خورد و زمان‌بندی متوقف شد؛ لاگ را بررسی و برنامه را دوباره باز کنید",Color.Firebrick);});
            try { journal.Append("application_error",new { error_type=exception.GetType().Name,cleanup_errors=errors }); } catch (Exception error) { journal.TryFailureMarker("application_error",error); }
        }
        private static void CleanupStep(List<string> errors,string scope,Action action){try{action();}catch(Exception error){errors.Add(scope+":"+TimingSafety.ErrorKinds(error));}}
        private void HandleClosed(object sender, FormClosedEventArgs e) { CloseResources(); }
        private void CloseResources() {
            if(Interlocked.Exchange(ref resourcesClosed,1)!=0)return;
            uiHealth.InvalidateWithReason("closed");
            List<string> errors=new List<string>();
            CleanupStep(errors,"cancel",delegate{SignalCancellation("closed");});
            initialized=false;pickerRestorePending=false;
            CleanupStep(errors,"timer_stop",timer.Stop);CleanupStep(errors,"timer_dispose",timer.Dispose);CleanupStep(errors,"escape",ReleaseEscape);
            bool unresolved=resultRecordUnresolved;string shutdownError=null;ClickSchedule current=clickSchedule;
            if(current!=null){
                bool accepted=current.Cancel("closed");
                try{current.Dispose();}catch(Exception error){shutdownError=TimingSafety.ErrorKinds(error);}
                ClickResult ready=current.CompletedResult;
                if(ready!=null)unresolved=!RecordResultOnce(ready,current);
                else{
                    unresolved=true;
                    try{journal.Append("shutdown_pending",new{schedule_id=current.Id,error=shutdownError??current.ShutdownError,worker_active=current.IsWorkerActive,phase=current.Phase,cancellation_accepted=accepted,outcome_unresolved=true,worker_result_missing_at_observation=true,destination_outcome_verified=false,native_input_count=(uint?)null});}catch(Exception error){journal.TryFailureMarker("shutdown_pending",error);}
                    // Publication can race the disk append. Reconcile a result
                    // that became available during it; pending is an observation.
                    ready=current.CompletedResult;if(ready!=null)unresolved=!RecordResultOnce(ready,current);
                }
            }
            SystemEvents.SessionSwitch-=SessionChanged; SystemEvents.PowerModeChanged-=PowerChanged; SystemEvents.DisplaySettingsChanged-=DisplaysChanged; SystemEvents.TimeChanged-=TimeChanged;
            try { journal.Append("closed",new { outcome_unresolved=unresolved,worker_result_record_unresolved=unresolved,destination_outcome_verified=false,shutdown_error=shutdownError,cleanup_errors=errors }); } catch (Exception error) { journal.TryFailureMarker("closed",error); }
            try{journal.Dispose();}catch(Exception error){journal.TryFailureMarker("journal_dispose",error);}
        }
        protected override void Dispose(bool disposing) {
            try{if(disposing)CloseResources();}
            finally{
                try{base.Dispose(disposing);}
                finally{if(disposing&&Interlocked.Exchange(ref fontsDisposed,1)==0){foreach(Font font in ownedFonts)font.Dispose();ownedFonts.Clear();}}
            }
        }
        private static string Reason(string reason) {
            switch (reason) {
                case "cancelled": case "escape": case "closed": return "زمان‌بندی لغو شد؛ کلیکی ارسال نشد";
                case "clock_change": case "scheduler_gap": case "power_changed": return "تغییر ساعت یا وقفه/خواب تشخیص داده شد؛ کلیک لغو شد";
                case "desktop_changed": case "target_moved": return "پنجره، نمایشگر یا مقیاس تغییر کرده است؛ مقصد را دوباره انتخاب کنید";
                case "session_changed": case "session_inactive": case "desktop_inactive": return "نشست یا دسکتاپ فعال نیست؛ کلیک لغو شد";
                case "recent_input": case "button_or_modifier_held": case "input_interference_after_commit": case "recent_input_after_commit": return "ورودی تازه یا کلید/دکمهٔ نگه‌داشته‌شده تشخیص داده شد؛ کلیکی ارسال نشد";
                case "escape_after_commit": return "Escape در بررسی نهایی تشخیص داده شد؛ کلیکی ارسال نشد";
                case "native_preflight_failed": return "آماده‌سازی نهایی ورودی ناموفق بود؛ کلیکی ارسال نشد";
                case "too_late": return "از حد تأخیر انتخاب‌شده گذشته است؛ کلیک لغو شد";
                case "target_not_reached": return "زمان هدف هنوز نرسیده است؛ کلیکی ارسال نشد";
                case "ui_unresponsive": case "ui_error": return "رابط برنامه پاسخ‌گو نبود؛ کلیک لغو شد";
                case "swapped_mouse_buttons_unsupported": return "چیدمان معکوس دکمه‌های موس پشتیبانی نشده است؛ مقصد فعال نشد";
                case "target_changed": case "target_unavailable": return "مقصد تغییر کرده یا در دسترس نیست؛ دوباره انتخاب کنید";
                case "target_integrity_higher": return "سطح دسترسی مقصد از این برنامه بالاتر است؛ زمان‌بندی فعال نشد";
                case "target_integrity_unknown": return "سطح دسترسی مقصد قابل بررسی نیست؛ زمان‌بندی فعال نشد";
                case "timer_high_resolution_unavailable": return "تایمر دقت‌بالای ویندوز در دسترس نیست؛ زمان‌بندی فعال نشد";
                case "too_late_after_commit": return "نمونهٔ نهایی از حد تأخیر گذشته بود؛ ورودی موس ارسال نشد";
                case "target_not_reached_after_commit": return "نمونهٔ نهایی هنوز پیش از هدف بود؛ ورودی موس ارسال نشد";
                case "ui_unresponsive_after_commit": return "سلامت رابط در کنترل نهایی تأیید نشد؛ ورودی موس ارسال نشد";
                case "input_failed_or_partial": case "input_exception_ambiguous": case "input_result_invalid": return "ارسال ورودی ناقص یا مبهم است؛ نتیجه را بررسی کنید. تکرار خودکار ندارد";
                default: return "وضعیت قابل تأیید نبود؛ کلیک متوقف شد ("+reason+")";
            }
        }
    }
    public static class ClockApp {
        [STAThread] public static void Main(string[] args) {
            string workspace=null,receipt=null,nonce=null; bool smoke=false;
            try {
                if (args.Length<1 || !Directory.Exists(args[0])) throw new ArgumentException("Existing workspace required."); workspace=Path.GetFullPath(args[0]);
                for (int i=1;i<args.Length;i++) {
                    if (args[i]=="--smoke-test") smoke=true;
                    else if (args[i]=="--launch-receipt" && i+1<args.Length) receipt=args[++i];
                    else if (args[i]=="--launch-nonce" && i+1<args.Length) nonce=args[++i];
                    else throw new ArgumentException("Unknown startup argument.");
                }
                Guid token;
                if ((receipt==null)!=(nonce==null) || (receipt!=null && (!TimingSafety.IsInside(receipt,TimingSafety.PrivateFolder(workspace)) || !Guid.TryParseExact(nonce,"N",out token)))) throw new ArgumentException("Invalid startup receipt.");
                if(receipt!=null)TimingSafety.RequireSafePath(receipt,workspace);
                DesktopInput.SetDpiMode(); TargetGuard.ActiveDesktop(); Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                using (ApplicationLease instance=new ApplicationLease()) {
                    ClockWindow window=new ClockWindow(workspace,smoke);
                    if(receipt!=null)window.RequireStartupConfirmation(receipt,nonce);
                    Application.ThreadException += delegate(object sender,ThreadExceptionEventArgs e) { window.FailClosed(e.Exception); };
                    window.Ready += delegate {
                        if (receipt!=null) {
                            TimingSafety.RequireSafePath(receipt,workspace);
                            TimingSafety.RequireSafePath(receipt+".abort",workspace);
                            if (File.Exists(receipt+".abort")) throw new TimeoutException("Launcher startup deadline already expired.");
                            string exe=Process.GetCurrentProcess().MainModule.FileName;
                            string json=TimingSafety.Json(new { status="ready",nonce=nonce,pid=Process.GetCurrentProcess().Id,version=TimingSafety.Version,exe=exe,exe_sha256=TimingSafety.FileHash(exe),armed=false,utc_accuracy_verified=false });
                            using (FileStream stream=new FileStream(receipt,FileMode.CreateNew,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)) { byte[] bytes=new UTF8Encoding(false).GetBytes(json); stream.Write(bytes,0,bytes.Length); stream.Flush(true); }
                        }
                    };
                    window.StartupFailed+=delegate(Exception error) {
                        Environment.ExitCode=1;
                        if(receipt!=null)try{TimingSafety.RequireSafePath(receipt,workspace);using(FileStream file=new FileStream(receipt,FileMode.CreateNew,FileAccess.Write,FileShare.Read)){byte[] bytes=new UTF8Encoding(false).GetBytes(TimingSafety.Json(new {status="error",nonce=nonce,message=error.Message,error_type=error.GetType().Name}));file.Write(bytes,0,bytes.Length);file.Flush(true);}}catch(Exception){}
                    };
                    Application.Run(window); if (smoke && !window.CueFired) Environment.ExitCode=1;
                }
            } catch (Exception ex) {
                Environment.ExitCode=1;
                if (receipt!=null && workspace!=null && TimingSafety.IsInside(receipt,TimingSafety.PrivateFolder(workspace))) {
                    try { TimingSafety.RequireSafePath(receipt,workspace); byte[] errorBytes=new UTF8Encoding(false).GetBytes(TimingSafety.Json(new { status="error",nonce=nonce,error_type=ex.GetType().Name,message=ex.Message })); using (FileStream errorStream=new FileStream(receipt,FileMode.CreateNew,FileAccess.Write,FileShare.Read)) {errorStream.Write(errorBytes,0,errorBytes.Length);errorStream.Flush(true);} } catch (Exception) { }
                } else MessageBox.Show(ex.Message,"Timing Clock could not start",MessageBoxButtons.OK,MessageBoxIcon.Error);
            }
        }
    }
}
