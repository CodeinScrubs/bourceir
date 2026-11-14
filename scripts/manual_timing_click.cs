// General one-shot desktop input. No account, browser, URL or broker API.
using System;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace BrsManualClock {
    public static class DesktopInput {
        [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] private struct MouseInput { public int dx, dy; public uint mouseData, flags, time; public UIntPtr extra; }
        [StructLayout(LayoutKind.Explicit)] private struct InputUnion { [FieldOffset(0)] public MouseInput mouse; }
        [StructLayout(LayoutKind.Sequential)] private struct Input { public uint type; public InputUnion data; }
        private static readonly int InputSize = Marshal.SizeOf(typeof(Input));
        public static int NativeInputSize { get { return InputSize; } }
        public static int NativeMouseInputSize { get { return Marshal.SizeOf(typeof(MouseInput)); } }
        static DesktopInput() {
            if (InputSize != (IntPtr.Size == 8 ? 40 : 28) || NativeMouseInputSize != (IntPtr.Size == 8 ? 32 : 24))
                throw new InvalidOperationException("Windows INPUT layout does not match the current process architecture.");
        }
        private static readonly int[] HeldKeys = { 1, 2, 4, 5, 6, 16, 17, 18, 91, 92 };
        [DllImport("kernel32.dll")] private static extern void GetSystemTimePreciseAsFileTime(out long time);
        [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
        [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point point);
        [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flag);
        [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, Input[] inputs, int size);
        [DllImport("user32.dll", SetLastError = true)] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] private static extern IntPtr GetThreadDpiAwarenessContext();
        [DllImport("user32.dll")] private static extern int GetAwarenessFromDpiAwarenessContext(IntPtr context);
        public static DateTimeOffset PreciseUtcNow() { long value; GetSystemTimePreciseAsFileTime(out value); return new DateTimeOffset(DateTime.FromFileTimeUtc(value)); }
        internal static double MonotonicMilliseconds { get { return Stopwatch.GetTimestamp()*1000.0/Stopwatch.Frequency; } }
        public static void SetDpiMode() {
            SetProcessDpiAwarenessContext(new IntPtr(-4));
            if (GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext()) != 2) throw new InvalidOperationException("Per-monitor DPI awareness is required. Windows 10 1803 or later is required.");
        }
        public static Point CursorPoint() { Point p; if (!GetCursorPos(out p)) throw new InvalidOperationException("Cursor unavailable."); return p; }
        public static Rectangle VirtualDesktop() { return new Rectangle(GetSystemMetrics(76), GetSystemMetrics(77), GetSystemMetrics(78), GetSystemMetrics(79)); }
        public static IntPtr RootOf(IntPtr window) { return window==IntPtr.Zero?IntPtr.Zero:GetAncestor(window,2); }
        public static IntPtr RootAt(Point p) { return RootOf(WindowFromPoint(p)); }
        public static bool EscapeDown() { return (GetAsyncKeyState(27) & 0x8000) != 0; }
        public static bool AnyButtonOrModifierDown() {
            foreach (int key in HeldKeys) if ((GetAsyncKeyState(key) & 0x8000) != 0) return true;
            return false;
        }
        public static int NormalizeAbsolute(int coordinate, int origin, int span) {
            if (span <= 0 || span > 65536 || coordinate < origin || (long)coordinate >= (long)origin + span) throw new ArgumentOutOfRangeException("coordinate");
            return Math.Max(0, Math.Min(65535, (int)(((coordinate - (double)origin) + 0.5) * 65536.0 / span)));
        }
        private static Input[] ToNative(MousePacket[] packets) {
            Input[] input = new Input[packets.Length];
            for (int i = 0; i < packets.Length; i++) { input[i].data.mouse.dx = packets[i].X; input[i].data.mouse.dy = packets[i].Y; input[i].data.mouse.flags = packets[i].Flags; }
            return input;
        }
        private sealed class WindowsNativeBoundary : INativeBoundary {
            private readonly Input[] input;
            internal WindowsNativeBoundary(Input[] input){this.input=input;}
            public DateTimeOffset Now(){return PreciseUtcNow();}
            public double Monotonic {get{return MonotonicMilliseconds;}}
            public uint LastInput {get{return TargetGuard.LastInputTick();}}
            public string Safety(ClickPlan plan){
                string changed=TargetGuard.ValidateNearInput(plan.ExpectedSnapshot);if(changed!=null)return changed;
                if(AnyButtonOrModifierDown())return "input_interference_after_commit";
                if(EscapeDown())return "escape_after_commit";
                uint last=TargetGuard.LastInputTick(),current=unchecked((uint)Environment.TickCount),age=unchecked(current-last);
                if(age>Int32.MaxValue||age<plan.ExpectedSnapshot.QuietMilliseconds)return "recent_input_after_commit";
                return null;
            }
            public RawSend Invoke(){
                long began=Stopwatch.GetTimestamp();
                uint sent=SendInput((uint)input.Length,input,InputSize);
                int error=sent==input.Length?0:Marshal.GetLastWin32Error();
                long returned=Stopwatch.GetTimestamp();
                RawSend result=new RawSend(sent,error);result.NativeDurationTicks=returned-began;return result;
            }
        }
        internal sealed class NativeBatch {
            private readonly Input[] click, release;
            private readonly ClickPlan plan;
            private readonly INativeBoundary clickBoundary,releaseBoundary;
            internal NativeBatch(MousePacket[] packets,ClickPlan selectedPlan) { plan=selectedPlan;click=ToNative(packets); release=ToNative(new MousePacket[]{new MousePacket{Flags=4}});clickBoundary=new WindowsNativeBoundary(click);releaseBoundary=new WindowsNativeBoundary(release); }
            internal RawSend Click(DispatchCommit commit) { return NativeBoundaryDispatch.Execute(plan,clickBoundary,commit); }
            internal RawSend Release() { return NativeBoundaryDispatch.Execute(null,releaseBoundary); }
            internal int Count { get { return click.Length; } }
        }
    }
    internal struct MousePacket { public int X, Y; public uint Flags; }
    internal struct RawSend {
        public readonly uint Count;public readonly int Error;
        public bool NativeInvoked,NativeBoundaryKnown;
        public string SkipReason,TimingError;
        public DateTimeOffset? PreNativeCheckAt,NativeEntrySample,NativeReturnSample;
        public long? NativeDurationTicks;
        public double? SafetyDurationMs,CommitElapsedMs;
        public RawSend(uint count,int error){this=default(RawSend);Count=count;Error=error;NativeInvoked=true;}
        internal static RawSend Skipped(string reason,DateTimeOffset? sample){RawSend r=new RawSend(0,0);r.NativeInvoked=false;r.NativeBoundaryKnown=true;r.SkipReason=reason;r.PreNativeCheckAt=sample;return r;}
    }
    // Internal fixture seam only. The shipped path owns a sealed Windows
    // implementation and prebuilds it before waiting; no public callback here.
    internal interface INativeBoundary {DateTimeOffset Now();double Monotonic{get;}uint LastInput{get;}string Safety(ClickPlan plan);RawSend Invoke();}
    // Created only by the scheduler's successful CAS. Wall time, QPC time and
    // last-input identity travel together; a larger lateness budget cannot
    // relax the independent 250 ms gap / 100 ms clock-continuity policies.
    internal sealed class DispatchCommit {
        internal readonly ClickPlan Plan;
        internal readonly DateTimeOffset OriginWall,Wall;
        internal readonly double OriginMono,Mono;
        internal readonly uint AcceptedLastInput;
        internal DispatchCommit(ClickPlan plan,DateTimeOffset originWall,double originMono,DateTimeOffset wall,double mono,uint lastInput){Plan=plan;OriginWall=originWall;OriginMono=originMono;Wall=wall;Mono=mono;AcceptedLastInput=lastInput;}
        internal string Validate(ClickPlan plan,DateTimeOffset now,double mono){
            if(!Object.ReferenceEquals(plan,Plan))return "commit_context_invalid";
            double elapsed=mono-Mono,originElapsed=mono-OriginMono;
            if(!TimingSafety.Finite(mono)||!TimingSafety.Finite(elapsed)||!TimingSafety.Finite(originElapsed)||originElapsed<0||Math.Abs((now-OriginWall).TotalMilliseconds-originElapsed)>100)return "clock_change_after_commit";
            string check=ClickPlan.ValidateTime(now,plan.Target,plan.MaximumLatenessMs,(now-Wall).TotalMilliseconds,elapsed);
            return check=="due"?null:check=="wait"?"target_not_reached_after_commit":check+"_after_commit";
        }
    }
    internal sealed class NativeInvocationException : Exception {
        internal readonly DateTimeOffset? Pre,Entry;internal readonly double? SafetyDuration,CommitElapsed;
        internal NativeInvocationException(Exception error,DateTimeOffset? pre,DateTimeOffset? entry,double? safety,double? elapsed):base("Native invocation returned no reliable input count.",error){Pre=pre;Entry=entry;SafetyDuration=safety;CommitElapsed=elapsed;}
    }
    internal static class NativeBoundaryDispatch {
        internal static RawSend Execute(ClickPlan plan,INativeBoundary boundary){return Execute(plan,boundary,null);}
        private static string Health(ClickPlan plan){string reason=plan.HealthState==null?null:plan.HealthState.RejectionReason(plan.HealthGeneration);return reason=="ui_unresponsive"?"ui_unresponsive_after_commit":reason;}
        internal static RawSend Execute(ClickPlan plan,INativeBoundary boundary,DispatchCommit commit){
            DateTimeOffset? pre=null,entry=null;string timing=null;double? safety=null,elapsed=null;
            if(plan!=null){
                try{
                    if(commit==null||!Object.ReferenceEquals(commit.Plan,plan))return RawSend.Skipped("commit_context_invalid",null);
                    string rejected=Health(plan);if(rejected!=null)return RawSend.Skipped(rejected,null);
                    pre=boundary.Now();double began=boundary.Monotonic;rejected=commit.Validate(plan,pre.Value,began);if(rejected!=null)return RawSend.Skipped(rejected,pre);
                    if(boundary.LastInput!=commit.AcceptedLastInput)return RawSend.Skipped("recent_input_after_commit",pre);
                    rejected=boundary.Safety(plan);if(rejected!=null)return RawSend.Skipped(rejected,pre);
                    rejected=Health(plan);if(rejected!=null)return RawSend.Skipped(rejected,pre);
                    if(boundary.LastInput!=commit.AcceptedLastInput)return RawSend.Skipped("recent_input_after_commit",pre);
                    entry=boundary.Now();double finalMono=boundary.Monotonic;safety=finalMono-began;elapsed=finalMono-commit.Mono;
                    rejected=commit.Validate(plan,entry.Value,finalMono);if(rejected!=null){RawSend skip=RawSend.Skipped(rejected,entry);skip.SafetyDurationMs=safety;skip.CommitElapsedMs=elapsed;return skip;}
                }catch(Exception error){RawSend failed=RawSend.Skipped("native_preflight_failed",pre);failed.TimingError="native_preflight:"+error.GetType().Name;return failed;}
            }else{
                // Known release-only cleanup must not be blocked by a failed
                // clock read or by guards intended for a new click.
                try{pre=entry=boundary.Now();}catch(Exception error){timing="release_entry:"+error.GetType().Name;}
            }
            // From this boundary onwards an exception has an unknown input
            // count. The dispatcher preserves ambiguity and never retries it.
            RawSend result;
            try{result=boundary.Invoke();}catch(Exception error){throw new NativeInvocationException(error,pre,entry,safety,elapsed);}
            result.NativeBoundaryKnown=true;result.PreNativeCheckAt=pre;result.NativeEntrySample=entry;result.SafetyDurationMs=safety;result.CommitElapsedMs=elapsed;
            try{result.NativeReturnSample=boundary.Now();}catch(Exception error){timing=timing==null?"native_return:"+error.GetType().Name:timing+";native_return:"+error.GetType().Name;}
            result.TimingError=timing;return result;
        }
    }
    internal interface IMouseQueue { RawSend Send(MousePacket[] packets); }
    internal interface IPreparedMouseQueue { RawSend Click(); RawSend Release(); }
    internal interface ICommittedMouseQueue { RawSend Click(DispatchCommit commit);RawSend Release(); }
    internal sealed class NativeMouseQueue : IMouseQueue, ICommittedMouseQueue {
        private readonly DesktopInput.NativeBatch batch;
        public NativeMouseQueue(ClickPlan plan) { batch=new DesktopInput.NativeBatch(InputDispatcher.Packets(plan),plan); }
        public RawSend Send(MousePacket[] packets) { throw new InvalidOperationException("Prepared native queue requires its owned batch."); }
        public RawSend Click(DispatchCommit commit) { return batch.Click(commit); }
        public RawSend Release() { return batch.Release(); }
    }

    public sealed class ClickPlan {
        public const double MaximumHorizonMilliseconds=24.0*60*60*1000;
        public readonly DateTimeOffset Target;
        private readonly TargetSnapshot snapshot;
        public TargetSnapshot Snapshot { get { return snapshot.Copy(); } }
        internal TargetSnapshot ExpectedSnapshot { get { return snapshot; } }
        public readonly double MaximumLatenessMs;
        public readonly bool Rehearsal;
        // Compatibility hook: pre-capture only. Final recurring UI health
        // requires HealthState; arbitrary code never runs after capture.
        public readonly Func<bool> UiHealthy;
        public readonly UiHealthState HealthState;
        public readonly long HealthGeneration;
        public DesktopInput.Point Point { get { return snapshot.Point; } }
        public Rectangle Desktop { get { return snapshot.DesktopBounds; } }
        public IntPtr RootWindow { get { return snapshot.Root; } }
        public int MinimumLeadMs { get { return Math.Max(1000, snapshot.QuietMilliseconds + 250); } }
        public ClickPlan(DateTimeOffset target, TargetSnapshot snapshot, double maximumLatenessMs, Func<bool> uiHealthy = null, bool rehearsal = false,UiHealthState healthState=null,long healthGeneration=0) {
            if (snapshot == null || snapshot.Root == IntPtr.Zero || snapshot.Leaf == IntPtr.Zero || snapshot.DesktopBounds.Width>65536 || snapshot.DesktopBounds.Height>65536 || !snapshot.DesktopBounds.Contains(snapshot.Point.X, snapshot.Point.Y) || snapshot.QuietMilliseconds < 1 || snapshot.QuietMilliseconds > 10000 || !TimingSafety.Finite(maximumLatenessMs) || maximumLatenessMs < 1 || maximumLatenessMs > 5000) throw new ArgumentException("Invalid click plan.");
            Target = target; this.snapshot = snapshot.Copy(); MaximumLatenessMs = maximumLatenessMs; UiHealthy = uiHealthy; Rehearsal=rehearsal;HealthState=healthState;HealthGeneration=healthGeneration;
        }
        public ClickPlan(DateTimeOffset target, DesktopInput.Point point, IntPtr root, Rectangle desktop, double maximumLatenessMs) : this(target, TargetGuard.Capture(point, IntPtr.Zero), maximumLatenessMs) {
            if (Snapshot.Root != root || Snapshot.DesktopBounds != desktop) throw new TargetGuardException("target_changed");
        }
        public static string ValidateTime(DateTimeOffset now, DateTimeOffset target, double maxLateMs, double wallDeltaMs, double monoDeltaMs) {
            if (!TimingSafety.Finite(maxLateMs) || !TimingSafety.Finite(wallDeltaMs) || !TimingSafety.Finite(monoDeltaMs) || maxLateMs < 1 || maxLateMs > 5000) return "invalid_timing";
            if (monoDeltaMs < 0 || Math.Abs(wallDeltaMs - monoDeltaMs) > 100) return "clock_change";
            if (monoDeltaMs > 250) return "scheduler_gap";
            if ((now - target).TotalMilliseconds > maxLateMs) return "too_late";
            return now >= target ? "due" : "wait";
        }
    }
    public sealed class ClickResult {
        public string ScheduleId, Outcome, TimingError, CleanupError;
        public DateTimeOffset Target;
        public DateTimeOffset? ObservedAt, DispatchCheckAt, SendStarted, SendReturned, RehearsalObservedAt;
        public DateTimeOffset? LastTimingCheckAt,QueueCallStarted,QueueCallReturned,PreNativeCheckAt,NativeCallEntrySample,NativeCallReturnSample,ReleaseEntrySample,ReleaseReturnSample,DispatchFinishedAt;
        public long? NativeCallDurationTicks,ReleaseDurationTicks;
        public double? NativeCallLatenessMs,GuardValidationDurationMs,NativeSafetyDurationMs,CommitToNativeSampleMs;
        public string SendTimingBasis;
        public bool CommitAccepted;
        public bool? NativeCallInvoked=false;
        public int PreCommitWaits;
        internal string CommitRejection;
        internal DispatchCommit CommitContext;
        public double? LatenessMs;
        public uint? QueuedEvents = 0, ReleaseEvents = 0;
        public int? NativeError;
        public bool? HighResolutionTimer;
        public bool DispatchAttempted, Rehearsal;
        private readonly object errorGate=new object();
        internal void AddCleanup(string error) { lock(errorGate){CleanupError = CleanupError == null ? error : CleanupError + ";" + error;} }
        internal void AddTiming(string error) { lock(errorGate){TimingError = TimingError == null ? error : TimingError + ";" + error;} }
    }
    internal static class InputDispatcher {
        internal static MousePacket[] Packets(ClickPlan plan) {
            return new MousePacket[] {
                new MousePacket { X = DesktopInput.NormalizeAbsolute(plan.Point.X, plan.Desktop.Left, plan.Desktop.Width), Y = DesktopInput.NormalizeAbsolute(plan.Point.Y, plan.Desktop.Top, plan.Desktop.Height), Flags = 0x8000 | 0x4000 | 0x2000 | 0x0001 },
                new MousePacket { Flags = 0x0002 }, new MousePacket { Flags = 0x0004 }
            };
        }
        public static void Dispatch(ClickPlan plan, ClickResult result, Func<DateTimeOffset> now, IMouseQueue sink, Func<bool> tryCommit) {
            if(tryCommit==null)throw new ArgumentNullException("tryCommit");
            try{
            IPreparedMouseQueue prepared = sink as IPreparedMouseQueue;
            ICommittedMouseQueue committed=sink as ICommittedMouseQueue;
            MousePacket[] packets = plan.Rehearsal || prepared!=null || committed!=null ? null : Packets(plan);
            result.Rehearsal=plan.Rehearsal;
            DateTimeOffset start = now();
            double late = (start - plan.Target).TotalMilliseconds;
            result.DispatchCheckAt = start; result.LatenessMs = late;
            if (late < 0) { result.Outcome = "target_not_reached"; return; }
            if (late > plan.MaximumLatenessMs) { result.Outcome = "too_late"; return; }
            // The scheduler's cancellation gate is the final managed operation before
            // committing to input. A time rejection never commits the schedule.
            if (!tryCommit()) { result.Outcome = result.CommitRejection ?? "cancelled"; return; }
            result.CommitAccepted=true;
            if (plan.Rehearsal) { result.Outcome="rehearsal_due";result.RehearsalObservedAt=result.DispatchCheckAt;result.QueuedEvents=0;return; }
            DateTimeOffset queueStart;
            try{queueStart=now();}catch(Exception error){result.Outcome="queue_preflight_failed";result.AddTiming("queue_entry:"+error.GetType().Name);return;}
            result.PreNativeCheckAt=queueStart;
            double queueLate=(queueStart-plan.Target).TotalMilliseconds;
            if(queueLate<0){result.Outcome="target_not_reached_after_commit";return;}
            if(queueLate>plan.MaximumLatenessMs){result.Outcome="too_late_after_commit";return;}
            result.SendStarted=queueStart;result.QueueCallStarted=queueStart;result.SendTimingBasis="managed_queue_wrapper_sample";result.DispatchAttempted = true;
            result.QueuedEvents = null;
            result.NativeCallInvoked=null;
            try {
                RawSend sent = committed!=null?committed.Click(result.CommitContext):prepared!=null?prepared.Click():sink.Send(packets);
                result.QueuedEvents = sent.Count; result.NativeError = sent.Error;
                result.NativeCallInvoked=sent.NativeBoundaryKnown?(bool?)sent.NativeInvoked:null;
                try{result.QueueCallReturned=now();}catch(Exception ex){result.AddTiming("queue_return:"+ex.GetType().Name);}
                result.PreNativeCheckAt=sent.NativeBoundaryKnown?sent.PreNativeCheckAt:queueStart;
                result.NativeCallEntrySample=sent.NativeEntrySample;result.NativeCallReturnSample=sent.NativeReturnSample;result.NativeCallDurationTicks=sent.NativeDurationTicks;
                result.NativeSafetyDurationMs=sent.SafetyDurationMs;result.CommitToNativeSampleMs=sent.CommitElapsedMs;
                if(sent.TimingError!=null)result.AddTiming(sent.TimingError);
                if(sent.NativeEntrySample.HasValue){result.SendStarted=sent.NativeEntrySample;result.SendReturned=sent.NativeReturnSample;result.NativeCallLatenessMs=(sent.NativeEntrySample.Value-plan.Target).TotalMilliseconds;result.SendTimingBasis="adjacent_native_boundary_samples";}
                else result.SendReturned=result.QueueCallReturned;
                if(!sent.NativeInvoked){result.DispatchAttempted=false;result.NativeError=null;result.SendStarted=null;result.SendReturned=null;result.SendTimingBasis="native_call_skipped";result.Outcome=sent.SkipReason??"native_call_skipped";if(sent.NativeBoundaryKnown)result.QueuedEvents=0;}
                else {
                result.Outcome = sent.Count == 3 ? "queued" : "input_failed_or_partial";
                if (sent.Count > 3) { result.QueuedEvents = null; result.Outcome = "input_result_invalid"; }
                if (sent.Count == 2) {
                    result.ReleaseEvents = null;
                    try {
                        RawSend release=committed!=null?committed.Release():prepared!=null?prepared.Release():sink.Send(new MousePacket[] { new MousePacket { Flags = 0x0004 } });uint released=release.Count;
                        result.ReleaseEntrySample=release.NativeEntrySample;result.ReleaseReturnSample=release.NativeReturnSample;result.ReleaseDurationTicks=release.NativeDurationTicks;
                        if(release.TimingError!=null)result.AddTiming(release.TimingError);
                        if(released>1){result.ReleaseEvents=null;result.AddCleanup("invalid_release_count");}
                        else { result.ReleaseEvents=released; if(released==0)result.AddCleanup("release_not_queued"); }
                    }
                    catch (Exception ex) { result.AddCleanup("release:" + ex.GetType().Name); }
                }
                }
            } catch (Exception ex) {
                // A thrown call has no reliable queued count. Do not inject another
                // event or release a potentially user-owned held button on a guess.
                NativeInvocationException native=ex as NativeInvocationException;
                if(native!=null){result.PreNativeCheckAt=native.Pre;result.NativeCallEntrySample=native.Entry;result.NativeSafetyDurationMs=native.SafetyDuration;result.CommitToNativeSampleMs=native.CommitElapsed;
                    result.SendStarted=native.Entry;result.SendReturned=null;result.SendTimingBasis="adjacent_preinvoke_sample_exception_unknown_count";
                    if(native.Entry.HasValue)result.NativeCallLatenessMs=(native.Entry.Value-plan.Target).TotalMilliseconds;}
                result.Outcome = "input_exception_ambiguous"; result.AddCleanup("dispatch:" + (native==null?ex.GetType().Name:native.InnerException.GetType().Name));
            }
            }finally{try { result.DispatchFinishedAt = now(); } catch (Exception ex) { result.AddTiming("dispatch_finished:"+ex.GetType().Name); }}
        }
    }
    internal sealed class PreciseWait : WaitHandle {
        internal const uint HighResolutionFlag = 0x00000002; // CREATE_WAITABLE_TIMER_HIGH_RESOLUTION; manual reset is 0x1.
        private sealed class WaitPair { internal readonly WaitHandle Cancel;internal readonly WaitHandle[] Handles;internal WaitPair(WaitHandle cancel,WaitHandle timer){Cancel=cancel;Handles=new WaitHandle[]{cancel,timer};} }
        private WaitPair cachedPair;
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateWaitableTimerEx(IntPtr attrs, string name, uint flags, uint access);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetWaitableTimer(SafeWaitHandle timer, ref long dueTime, int period, IntPtr callback, IntPtr state, bool resume);
        public bool Available { get { return SafeWaitHandle != null && !SafeWaitHandle.IsInvalid; } }
        public readonly int CreationError;
        public PreciseWait() : this(delegate{return CreateWaitableTimerEx(IntPtr.Zero,null,HighResolutionFlag,0x00100002);},Marshal.GetLastWin32Error) { }
        internal PreciseWait(Func<IntPtr> create,Func<int> lastError) { IntPtr handle=create();CreationError=handle==IntPtr.Zero?lastError():0;SafeWaitHandle=new SafeWaitHandle(handle,true); }
        public void RequireHighResolution() { if(!Available)throw new TargetGuardException("timer_high_resolution_unavailable",CreationError); }
        public void Pause(double ms, WaitHandle cancel) {
            if (!TimingSafety.Finite(ms) || ms < 0 || ms > Int32.MaxValue) throw new ArgumentOutOfRangeException("ms");
            if (cancel == null) throw new ArgumentNullException("cancel");
            if (ms == 0) { Thread.SpinWait(32); return; }
            if (!Available) { cancel.WaitOne(Math.Max(1, (int)ms)); return; }
            long due = -Math.Max(1L, checked((long)(ms * 10000)));
            if (!SetWaitableTimer(SafeWaitHandle, ref due, 0, IntPtr.Zero, IntPtr.Zero, false)) throw new InvalidOperationException("Waitable timer failed.");
            WaitPair pair=Volatile.Read(ref cachedPair);
            if(pair==null||!Object.ReferenceEquals(pair.Cancel,cancel)){pair=new WaitPair(cancel,this);Volatile.Write(ref cachedPair,pair);}
            WaitHandle.WaitAny(pair.Handles);
        }
    }
    internal interface IClickEnvironment : IDisposable {
        DateTimeOffset Now(); double Monotonic { get; } uint CurrentTick { get; } uint LastInput { get; }
        bool Escape { get; } bool ButtonsHeld { get; } bool HighResolution { get; }
        string ValidateTarget(ClickPlan plan); void Pause(double ms, WaitHandle cancel); void Dispatch(ClickPlan plan, ClickResult result, Func<bool> tryCommit);
    }
    internal interface IExecutionScopeEnvironment { IDisposable BeginExecution(); }
    internal sealed class WindowsClickEnvironment : IClickEnvironment, IExecutionScopeEnvironment {
        private readonly PreciseWait wait;
        private readonly IMouseQueue mouse;
        private readonly Func<DateTimeOffset> clock;
        public WindowsClickEnvironment(ClickPlan plan) {
            InputPrivilege.Require(plan.ExpectedSnapshot);
            mouse=plan.Rehearsal?null:new NativeMouseQueue(plan);
            clock=Now; wait=new PreciseWait();
            try{wait.RequireHighResolution();}catch{wait.Dispose();throw;}
        }
        public IDisposable BeginExecution() { return new WindowsExecutionLease(); }
        public DateTimeOffset Now() { return DesktopInput.PreciseUtcNow(); }
        public double Monotonic { get { return DesktopInput.MonotonicMilliseconds; } }
        public uint CurrentTick { get { return unchecked((uint)Environment.TickCount); } }
        public uint LastInput { get { return TargetGuard.LastInputTick(); } }
        public bool Escape { get { return DesktopInput.EscapeDown(); } }
        public bool ButtonsHeld { get { return DesktopInput.AnyButtonOrModifierDown(); } }
        public bool HighResolution { get { return wait.Available; } }
        public string ValidateTarget(ClickPlan plan) { return TargetGuard.Validate(plan.ExpectedSnapshot); }
        public void Pause(double ms, WaitHandle cancel) { wait.Pause(ms, cancel); }
        public void Dispatch(ClickPlan plan, ClickResult result, Func<bool> tryCommit) { InputDispatcher.Dispatch(plan, result, clock, mouse, tryCommit); }
        public void Dispose() { wait.Dispose(); }
    }
    public sealed class ScheduleTimingException : ArgumentException {
        public readonly int RequiredLeadMs;
        public ScheduleTimingException(int requiredLead) : base("The target must be further in the future.") { RequiredLeadMs=requiredLead; }
    }
    public sealed class ScheduleHorizonException : ArgumentException {
        public ScheduleHorizonException():base("A click target must be within the next 24 hours."){}
    }
    public sealed class ClickSchedule : IDisposable {
        private const int Prepared = 0, Armed = 1, Cancelled = 2, Dispatching = 3, Completed = 4, RehearsalObserving = 5;
        private sealed class ScheduleState {
            internal readonly int Phase; internal readonly string Reason;
            internal readonly ClickResult Result;
            internal readonly DispatchCommit Commit;
            internal ScheduleState(int phase, string reason = null,ClickResult result=null,DispatchCommit commit=null) { Phase=phase; Reason=reason;Result=result;Commit=commit; }
        }
        private static readonly ScheduleState PreparedState=new ScheduleState(Prepared), ArmedState=new ScheduleState(Armed);
        private ScheduleState state=PreparedState;
        private readonly object startGate=new object();
        private readonly ManualResetEvent cancelSignal;
        private readonly Thread worker;
        private readonly ClickPlan plan;
        private readonly Action<ClickResult> completed;
        private readonly IClickEnvironment environment;
        private readonly IDisposable lease;
        private readonly DateTimeOffset preparedWall;
        private readonly double preparedMono;
        private int released, workerStarted, disposed, signalDisposed, disposeTimedOut,everDisposeTimedOut,notificationState;
        private string notificationError;
        public readonly string Id = Guid.NewGuid().ToString("N");
        public ClickResult CompletedResult { get { return Volatile.Read(ref state).Result; } }
        public string NotificationError { get { return Volatile.Read(ref notificationError); } }
        public bool CompletionCallbackPending { get { int value=Volatile.Read(ref notificationState);return value==1||value==2; } }
        // Compatibility alias; neither property measures UI message consumption.
        public bool NotificationPending { get { return CompletionCallbackPending; } }
        public bool HighResolutionTimer { get { return environment.HighResolution; } }
        public bool EverDisposeTimedOut { get { return Volatile.Read(ref everDisposeTimedOut)!=0; } }
        public string Phase { get { switch(Volatile.Read(ref state).Phase){case Prepared:return "prepared";case Armed:return "armed";case Cancelled:return "cancelled";case Dispatching:return "dispatching";case RehearsalObserving:return "rehearsal_observing";default:return "completed";} } }
        public bool IsPending { get { int current = Volatile.Read(ref state).Phase; return current == Prepared || current == Armed; } }
        public bool IsWorkerActive { get { return worker != null && worker.IsAlive; } }
        public bool DisposeTimedOut { get { return Volatile.Read(ref disposeTimedOut) != 0; } }
        public string ShutdownError { get { return DisposeTimedOut ? "worker_shutdown_timeout" : null; } }
        public ClickSchedule(ClickPlan selectedPlan, Action<ClickResult> onCompleted) : this(selectedPlan, onCompleted, null, null, true) { }
        public static ClickSchedule Prepare(ClickPlan plan, Action<ClickResult> onCompleted) { return new ClickSchedule(plan, onCompleted, null, null, false); }
        internal ClickSchedule(ClickPlan selectedPlan, Action<ClickResult> onCompleted, IClickEnvironment suppliedEnvironment, IDisposable suppliedLease, bool autoStart) {
            if (selectedPlan == null) throw new ArgumentNullException("selectedPlan");
            plan = selectedPlan; completed = onCompleted;
            cancelSignal=new ManualResetEvent(false);
            try {
                environment = suppliedEnvironment ?? new WindowsClickEnvironment(selectedPlan);
                lease = suppliedLease ?? new PlanLease();
                preparedWall=environment.Now(); preparedMono=environment.Monotonic;
                if((plan.Target-preparedWall).TotalMilliseconds>ClickPlan.MaximumHorizonMilliseconds)throw new ScheduleHorizonException();
                if ((plan.Target - preparedWall).TotalMilliseconds < plan.MinimumLeadMs) throw new ScheduleTimingException(plan.MinimumLeadMs);
                worker = new Thread(Run) { IsBackground = true, Name = "One shot click scheduler" };
                if (autoStart) Start();
            } catch {
                if (Volatile.Read(ref released)==0) {
                    try { if(environment!=null)environment.Dispose(); } catch (Exception) { }
                    try { if (lease != null) lease.Dispose(); } catch (Exception) { }
                }
                cancelSignal.Dispose(); throw;
            }
        }
        public void Start() {
            DateTimeOffset now=environment.Now(); double mono=environment.Monotonic;
            if (!TimingSafety.Finite(mono) || Math.Abs((now-preparedWall).TotalMilliseconds-(mono-preparedMono))>100) throw new TargetGuardException("clock_change");
            if ((plan.Target - now).TotalMilliseconds < plan.MinimumLeadMs) throw new ScheduleTimingException(plan.MinimumLeadMs);
            if((plan.Target-now).TotalMilliseconds>ClickPlan.MaximumHorizonMilliseconds)throw new ScheduleHorizonException();
            bool armedHere=false;
            try { lock(startGate) {
                if (Volatile.Read(ref disposed)!=0 || Interlocked.CompareExchange(ref state, ArmedState, PreparedState) != PreparedState) throw new InvalidOperationException("Schedule cannot be started twice or after cancellation/disposal.");
                armedHere=true;worker.Start(); Volatile.Write(ref workerStarted, 1);
            }} catch { if(armedHere && Volatile.Read(ref workerStarted)==0)Finish(new ClickResult { ScheduleId=Id,Target=plan.Target,Rehearsal=plan.Rehearsal,Outcome="worker_start_failed" });throw; }
        }
        private bool TryCommit(DispatchCommit commit) { return Interlocked.CompareExchange(ref state,new ScheduleState(plan.Rehearsal?RehearsalObserving:Dispatching,null,null,commit),ArmedState)==ArmedState; }
        private string CancellationReason { get { return Volatile.Read(ref state).Reason ?? "cancelled"; } }
        // True means this schedule cannot subsequently inject native input. It
        // also covers an already completed no-input result and a pure rehearsal.
        public bool Cancel() { return Cancel("cancelled"); }
        public bool Cancel(string reason) {
            for (;;) {
                ScheduleState current = Volatile.Read(ref state);
                if (current.Phase == Dispatching) return false;
                if (current.Phase == RehearsalObserving) return true;
                if (current.Phase == Completed) { ClickResult done = CompletedResult; return done != null && !done.DispatchAttempted; }
                if (current.Phase == Cancelled) return true;
                ScheduleState cancelled=new ScheduleState(Cancelled,reason ?? "cancelled");
                if (Interlocked.CompareExchange(ref state, cancelled, current) != current) continue;
                try { cancelSignal.Set(); } catch (ObjectDisposedException) { }
                if (current.Phase == Prepared) Finish(new ClickResult { ScheduleId = Id, Target = plan.Target, Rehearsal=plan.Rehearsal, Outcome = cancelled.Reason });
                return true;
            }
        }
        private void Run() {
            Volatile.Write(ref workerStarted,1);
            ClickResult outcome = new ClickResult { ScheduleId = Id, Target = plan.Target, Rehearsal=plan.Rehearsal, Outcome = "cancelled" };
            IDisposable executionScope=null;
            try {
                IExecutionScopeEnvironment scoped=environment as IExecutionScopeEnvironment;
                if(scoped!=null)executionScope=scoped.BeginExecution();
                outcome.HighResolutionTimer = environment.HighResolution;
                // Include journal/startup and execution-scope acquisition in continuity.
                DateTimeOffset origin = preparedWall, last = origin;
                double originMono = preparedMono, previousMono = originMono;
                InputActivity activity = new InputActivity();
                DateTimeOffset gateWall=origin;double gateMono=originMono;
                // This is owned scheduler code, not the caller's UiHealthy callback.
                Func<bool> guardedCommit=delegate {
                    if(plan.HealthState!=null&&!plan.HealthState.IsHealthy(plan.HealthGeneration)){outcome.CommitRejection=plan.HealthState.RejectionReason(plan.HealthGeneration)??"ui_unresponsive";return false;}
                    if(environment.ButtonsHeld){outcome.CommitRejection="button_or_modifier_held";return false;}
                    uint acceptedInput=environment.LastInput;activity.Observe(acceptedInput,environment.Monotonic);
                    if(!activity.Quiet(environment.CurrentTick,environment.Monotonic,plan.ExpectedSnapshot.QuietMilliseconds)){outcome.CommitRejection="recent_input";return false;}
                    if(environment.Escape){outcome.CommitRejection="escape";return false;}
                    DateTimeOffset sample=environment.Now();double mono=environment.Monotonic;
                    outcome.DispatchCheckAt=sample;outcome.LatenessMs=(sample-plan.Target).TotalMilliseconds;
                    string check=ClickPlan.ValidateTime(sample,plan.Target,plan.MaximumLatenessMs,(sample-gateWall).TotalMilliseconds,mono-gateMono);
                    if(Math.Abs((sample-origin).TotalMilliseconds-(mono-originMono))>100)check="clock_change";
                    if(check!="due"){outcome.CommitRejection=check=="wait"?"target_not_reached":check;return false;}
                    DispatchCommit ticket=new DispatchCommit(plan,origin,originMono,sample,mono,acceptedInput);
                    if(!TryCommit(ticket))return false;
                    outcome.CommitContext=ticket;return true;
                };
                for (;;) {
                    if (Volatile.Read(ref state).Phase == Cancelled) { outcome.Outcome = CancellationReason; break; }
                    if (environment.Escape) { outcome.Outcome="escape"; break; }
                    DateTimeOffset now = environment.Now(); double elapsed = environment.Monotonic;
                    outcome.LastTimingCheckAt=now;
                    string decision = ClickPlan.ValidateTime(now, plan.Target, plan.MaximumLatenessMs, (now - last).TotalMilliseconds, elapsed - previousMono);
                    if (Math.Abs((now - origin).TotalMilliseconds - (elapsed - originMono)) > 100) { outcome.Outcome = "clock_change"; break; }
                    if(plan.HealthState!=null&&!plan.HealthState.IsHealthy(plan.HealthGeneration)){outcome.Outcome=plan.HealthState.RejectionReason(plan.HealthGeneration)??"ui_unresponsive";break;}
                    if (plan.UiHealthy != null && !plan.UiHealthy()) { outcome.Outcome = "ui_unresponsive"; break; }
                    activity.Observe(environment.LastInput, elapsed);
                    if (decision != "wait") {
                        if (decision != "due") { outcome.Outcome = decision; outcome.DispatchCheckAt=now; outcome.LatenessMs=(now-plan.Target).TotalMilliseconds; break; }
                        if (environment.ButtonsHeld) { outcome.Outcome = "button_or_modifier_held"; break; }
                        activity.Observe(environment.LastInput, environment.Monotonic);
                        if (!activity.Quiet(environment.CurrentTick, environment.Monotonic, plan.ExpectedSnapshot.QuietMilliseconds)) { outcome.Outcome = "recent_input"; break; }
                        if (environment.Escape) { outcome.Outcome = "escape"; break; }
                        if (plan.UiHealthy != null && !plan.UiHealthy()) { outcome.Outcome = "ui_unresponsive"; break; }
                        // No application/user health callback follows this final
                        // target capture. Native check-to-input races still exist.
                        double guardBegan=environment.Monotonic;string guard = environment.ValidateTarget(plan);outcome.GuardValidationDurationMs=environment.Monotonic-guardBegan;
                        if(Volatile.Read(ref state).Phase==Cancelled){outcome.Outcome=CancellationReason;break;}
                        if (guard != null) { outcome.Outcome = guard; break; }
                        if(plan.HealthState!=null&&!plan.HealthState.IsHealthy(plan.HealthGeneration)){outcome.Outcome=plan.HealthState.RejectionReason(plan.HealthGeneration)??"ui_unresponsive";break;}
                        if (environment.ButtonsHeld) { outcome.Outcome = "button_or_modifier_held"; break; }
                        activity.Observe(environment.LastInput, environment.Monotonic);
                        if (!activity.Quiet(environment.CurrentTick, environment.Monotonic, plan.ExpectedSnapshot.QuietMilliseconds)) { outcome.Outcome = "recent_input"; break; }
                        if (environment.Escape) { outcome.Outcome = "escape"; break; }
                         // Target/session queries can block. Re-check continuity after those
                         // queries so a stall plus a clock correction cannot hide the gap.
                         DateTimeOffset checkedNow = environment.Now(); double checkedMono = environment.Monotonic;
                          outcome.LastTimingCheckAt=checkedNow;
                         string checkedDecision = ClickPlan.ValidateTime(checkedNow, plan.Target, plan.MaximumLatenessMs, (checkedNow - now).TotalMilliseconds, checkedMono - elapsed);
                         if (Math.Abs((checkedNow - origin).TotalMilliseconds - (checkedMono - originMono)) > 100) checkedDecision = "clock_change";
                          if(checkedDecision=="wait"){last=checkedNow;previousMono=checkedMono;continue;}
                          if (checkedDecision != "due") { outcome.Outcome = checkedDecision; outcome.DispatchCheckAt=checkedNow; outcome.LatenessMs=(checkedNow-plan.Target).TotalMilliseconds; break; }
                        gateWall=checkedNow;gateMono=checkedMono;
                        environment.Dispatch(plan, outcome, guardedCommit);
                        if(!outcome.CommitAccepted&&!outcome.DispatchAttempted&&outcome.Outcome=="target_not_reached"){
                            // This repeats only a known pre-commit no-input wait,
                            // never an attempted or ambiguous send. Keep the
                            // original cumulative continuity anchor intact.
                            outcome.PreCommitWaits++;outcome.CommitRejection=null;
                            last=checkedNow;previousMono=checkedMono;continue;
                        }
                        if (outcome.Outcome == "cancelled") outcome.Outcome=CancellationReason;
                        break;
                    }
                    last = now; previousMono = elapsed;
                    double remaining = (plan.Target - now).TotalMilliseconds;
                    environment.Pause(remaining > 2 ? Math.Min(remaining>5000?100:50, Math.Max(0.1, remaining - 1.5)) : 0, cancelSignal);
                }
            } catch (TargetGuardException ex) { outcome.Outcome = ex.Reason; }
            catch (Exception ex) { if (!outcome.DispatchAttempted) outcome.Outcome = "error_" + ex.GetType().Name; else outcome.AddCleanup("worker:" + ex.GetType().Name); }
            finally {
                if(executionScope!=null)try{executionScope.Dispose();}catch(Exception ex){
                    outcome.AddCleanup("execution_scope:" + ex.GetType().Name);
                    if(executionScope is WindowsExecutionLease)try{executionScope.Dispose();}catch(Exception retry){outcome.AddCleanup("execution_scope_retry:" + retry.GetType().Name);}
                }
            }
            try { outcome.ObservedAt = environment.Now(); } catch (Exception ex) { outcome.AddTiming(ex.GetType().Name); }
            Finish(outcome);
        }
        private void Finish(ClickResult outcome) {
            if (Interlocked.Exchange(ref released, 1) != 0) return;
            try { environment.Dispose(); } catch (Exception ex) { outcome.AddCleanup("environment:" + TimingSafety.ErrorKinds(ex)); }
            try { lease.Dispose(); } catch (Exception ex) { outcome.AddCleanup("lease:" + TimingSafety.ErrorKinds(ex)); }
            if(completed!=null)Volatile.Write(ref notificationState,1);
            // Publish terminal state and result together. A cancellation can
            // race a health/guard rejection or resource cleanup; if its CAS
            // won before completion, retain that published reason for no-input
            // outcomes. Never rewrite a committed/native outcome as cancelled.
            for(;;){
                ScheduleState current=Volatile.Read(ref state);
                if(current.Phase==Cancelled&&!outcome.CommitAccepted&&!outcome.DispatchAttempted&&outcome.Outcome!="worker_start_failed"&&!outcome.Outcome.StartsWith("error_",StringComparison.Ordinal))outcome.Outcome=current.Reason??"cancelled";
                ScheduleState done=new ScheduleState(Completed,null,outcome);
                if(Interlocked.CompareExchange(ref state,done,current)==current)break;
            }
            if(completed!=null){
                try{if(!ThreadPool.QueueUserWorkItem(delegate{
                    Volatile.Write(ref notificationState,2);
                    try{completed(outcome);}catch(Exception ex){Volatile.Write(ref notificationError,ex.GetType().Name);}
                    finally{Volatile.Write(ref notificationState,3);}
                })){Volatile.Write(ref notificationError,"notification_queue_failed");Volatile.Write(ref notificationState,3);}}
                catch(Exception ex){Volatile.Write(ref notificationError,ex.GetType().Name);Volatile.Write(ref notificationState,3);}
            }
            if (Volatile.Read(ref disposed)==1) DisposeSignal();
        }
        private void DisposeSignal() { if(Interlocked.Exchange(ref signalDisposed,1)==0)cancelSignal.Dispose(); }
        public void Dispose() {
            Interlocked.Exchange(ref disposed, 1);
            Cancel("closed");
            bool started; lock(startGate) { started=Volatile.Read(ref workerStarted)==1; }
            if (started && worker != Thread.CurrentThread) {
                if (worker.Join(1000)) { Volatile.Write(ref disposeTimedOut,0);DisposeSignal(); }
                else {Volatile.Write(ref everDisposeTimedOut,1);Volatile.Write(ref disposeTimedOut,1);}
            }
            else if (Volatile.Read(ref state).Phase == Completed) {Volatile.Write(ref disposeTimedOut,0);DisposeSignal();}
        }
    }
}
