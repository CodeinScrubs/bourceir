// Explicitly local integration fixture. Only this fixture's button can be targeted.
using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using BrsManualClock;

namespace BrsManualClock.Tests {
    public sealed class ClickFixture : Form {
        [StructLayout(LayoutKind.Sequential)]private struct TestMouse {public int X,Y;public uint Data,Flags,Time;public UIntPtr Extra;}
        [StructLayout(LayoutKind.Explicit)]private struct TestUnion {[FieldOffset(0)]public TestMouse Mouse;}
        [StructLayout(LayoutKind.Sequential)]private struct TestInput {public uint Type;public TestUnion Data;}
        [DllImport("user32.dll",SetLastError=true)]private static extern uint SendInput(uint count,TestInput[] inputs,int size);
        // Fixture-only movement: never a down/up packet, and only while our own
        // local window owns the destination point. No production raw-send API.
        private void MoveForRecentInput(Point point){
            DesktopInput.Point p=new DesktopInput.Point{X=point.X,Y=point.Y};if(DesktopInput.RootAt(p)!=Handle)throw new InvalidOperationException("Owned move target was covered.");
            Rectangle bounds=DesktopInput.VirtualDesktop();TestInput move=new TestInput();move.Data.Mouse.X=DesktopInput.NormalizeAbsolute(p.X,bounds.Left,bounds.Width);move.Data.Mouse.Y=DesktopInput.NormalizeAbsolute(p.Y,bounds.Top,bounds.Height);move.Data.Mouse.Flags=0x8000|0x4000|0x2000|1;
            if(SendInput(1,new[]{move},Marshal.SizeOf(typeof(TestInput)))!=1)throw new InvalidOperationException("Owned fixture move was not queued.");
        }
        private readonly Button button;
        private readonly Timer timer = new Timer();
        private readonly string outputPath, scenario;
        private readonly int trials;
        private int trial, downs, ups, clicks;
        private DateTimeOffset target, mouseDownAt, doneAt;
        private ClickSchedule schedule;
        private ClickResult result;
        private Form overlay;
        private bool startPending = true;
        private bool injectedMove;
        public bool Passed = true;
        public ClickFixture(string output, string selectedScenario, int count) {
            outputPath = output; scenario = selectedScenario; trials = count;
            Text = "LOCAL CLICK TEST ONLY - no broker";
            ClientSize = new Size(560, 260); TopMost = true; StartPosition = FormStartPosition.CenterScreen;
            Label caption = new Label { Text = "Harmless local click target. Test closes itself.", AutoSize = false, TextAlign = ContentAlignment.MiddleCenter };
            caption.SetBounds(20, 20, 520, 40); Controls.Add(caption);
            button = new Button { Text = "LOCAL TEST TARGET" }; button.SetBounds(130, 95, 300, 80); Controls.Add(button);
            button.MouseDown += delegate { downs++; mouseDownAt = DesktopInput.PreciseUtcNow(); };
            button.MouseUp += delegate { ups++; };
            button.Click += delegate { clicks++; };
            timer.Interval = 50;
            Shown += delegate { doneAt = DesktopInput.PreciseUtcNow().AddMilliseconds(300); timer.Start(); };
            timer.Tick += Check;
            FormClosed += delegate { timer.Stop(); if (schedule != null) schedule.Dispose(); if (overlay != null) overlay.Dispose(); };
        }
        private void StartTrial() {
            startPending = false; downs = ups = clicks = 0; result = null;injectedMove=false;
            button.Enabled=true;
            Point center = button.PointToScreen(new Point(button.Width / 2, button.Height / 2));
            DesktopInput.Point p = new DesktopInput.Point { X = center.X, Y = center.Y };
            if (DesktopInput.RootAt(p) != Handle) { Passed = false; Write("{\"guard\":\"fixture_not_at_point\"}"); Close(); return; }
            target = DesktopInput.PreciseUtcNow().AddSeconds(2);
            ClickPlan plan = new ClickPlan(target, p, Handle, DesktopInput.VirtualDesktop(), 100);
            schedule = new ClickSchedule(plan, delegate(ClickResult r) { if (!IsDisposed) BeginInvoke(new Action(delegate { result = r; doneAt = DesktopInput.PreciseUtcNow().AddMilliseconds(500); })); });
            if (scenario == "cancel") schedule.Cancel();
            if (scenario == "target_changed") {
                overlay = new Form { Text = "LOCAL OVERLAY TEST", TopMost = true, StartPosition = FormStartPosition.Manual, Location = PointToScreen(new Point(100, 70)), Size = new Size(360, 150) };
                overlay.Show();
            }
            if (scenario == "target_moved") Left+=20;
            if (scenario == "target_resized") Width+=20;
            if (scenario == "target_disabled") button.Enabled=false;
        }
        private void Check(object sender, EventArgs args) {
            DateTimeOffset now = DesktopInput.PreciseUtcNow();
            if (startPending && now >= doneAt) { StartTrial(); return; }
            if(scenario=="recent_input" && !injectedMove && target!=default(DateTimeOffset) && now>=target.AddMilliseconds(-200)) {
                Point local=button.PointToScreen(new Point(button.Width/2+1,button.Height/2));MoveForRecentInput(local);injectedMove=true;
            }
            if (result == null || now < doneAt) return;
            bool ok;
            // This is a fixture acceptance bound, not a Windows/destination guarantee.
            if (scenario == "success") ok = result.Outcome == "queued" && result.QueuedEvents == 3 && downs == 1 && ups == 1 && clicks == 1 && mouseDownAt >= target && (mouseDownAt-target).TotalMilliseconds<=100 && result.LatenessMs>=0 && result.LatenessMs<=100 && result.CommitAccepted && result.DispatchAttempted && result.QueueCallStarted.HasValue && result.NativeCallEntrySample.HasValue && result.NativeCallReturnSample.HasValue && result.NativeCallDurationTicks.HasValue && result.NativeCallDurationTicks.Value>=0 && result.SendStarted==result.NativeCallEntrySample && result.SendReturned==result.NativeCallReturnSample && result.NativeCallEntrySample>=result.QueueCallStarted && result.NativeCallReturnSample>=result.NativeCallEntrySample && result.DispatchFinishedAt>=result.SendReturned && result.NativeCallLatenessMs>=0 && result.NativeCallLatenessMs<=100 && result.SendTimingBasis=="adjacent_native_boundary_samples";
            else {
                string expected=scenario=="cancel"?"cancelled":scenario=="recent_input"?"recent_input":scenario=="target_moved" || scenario=="target_resized"?"target_moved":"target_changed";
                ok=(result.Outcome==expected || (scenario=="target_disabled" && result.Outcome=="target_unavailable")) && result.QueuedEvents==0 && clicks==0 && downs==0;
            }
            if(scenario=="success")ok &= result.NativeCallInvoked==true;
            Passed &= ok;
            Write(TimingSafety.Json(new { version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(now),scenario=scenario,trial=trial+1,passed=ok,outcome=result.Outcome,target_tehran=TimingSafety.Iso(target),queued_events=result.QueuedEvents,mouse_down_count=downs,mouse_up_count=ups,click_count=clicks,native_call_invoked=result.NativeCallInvoked,deadline_gate_lateness_ms=result.LatenessMs,guard_validation_duration_ms=result.GuardValidationDurationMs,native_boundary_lateness_ms=result.NativeCallLatenessMs,queue_call_started=result.QueueCallStarted.HasValue?TimingSafety.Iso(result.QueueCallStarted.Value):null,native_entry_sample=result.NativeCallEntrySample.HasValue?TimingSafety.Iso(result.NativeCallEntrySample.Value):null,native_return_sample=result.NativeCallReturnSample.HasValue?TimingSafety.Iso(result.NativeCallReturnSample.Value):null,native_call_duration_ms=result.NativeCallDurationTicks.HasValue?(double?)result.NativeCallDurationTicks.Value*1000/Stopwatch.Frequency:null,send_timing_basis=result.SendTimingBasis,dispatch_finished_at=result.DispatchFinishedAt.HasValue?TimingSafety.Iso(result.DispatchFinishedAt.Value):null,mouse_down_tehran=clicks==0?null:TimingSafety.Iso(mouseDownAt),receive_lateness_ms=clicks==0?(double?)null:(mouseDownAt-target).TotalMilliseconds,high_resolution_timer=result.HighResolutionTimer }));
            schedule.Dispose(); schedule = null;
            if (overlay != null) { overlay.Dispose(); overlay = null; }
            trial++;
            if (trial >= trials || !ok) { Close(); return; }
            startPending = true; doneAt = now.AddMilliseconds(300);
        }
        private void Write(string line) { File.AppendAllText(outputPath, line + "\n", new UTF8Encoding(false)); }
    }
    public static class ClickSmoke {
        [STAThread] public static void Main(string[] args) {
            if (args.Length != 3 || Array.IndexOf(new string[]{"success","cancel","target_changed","target_moved","target_resized","target_disabled","recent_input"},args[1])<0) throw new ArgumentException("Output path, test scenario and trial count required.");
            DesktopInput.SetDpiMode();
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            ClickFixture fixture = new ClickFixture(args[0], args[1], Int32.Parse(args[2], CultureInfo.InvariantCulture));
            Application.Run(fixture);
            Environment.ExitCode = fixture.Passed ? 0 : 1;
        }
    }
}
