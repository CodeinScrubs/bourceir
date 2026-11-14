// Fault injection is confined to tests. This suite never sends desktop input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using BrsManualClock;

namespace BrsManualClock.Tests {
    internal sealed class TestLease : IDisposable { public int Disposals; public bool ThrowOnDispose; public void Dispose() { Interlocked.Increment(ref Disposals); if (ThrowOnDispose) throw new IOException("test lease failure"); } }
    internal sealed class TestMouse : IMouseQueue {
        public readonly List<MousePacket[]> Calls=new List<MousePacket[]>();
        public uint Count=3; public bool ThrowOnSend,ThrowOnRelease; public Action OnSend;
        public RawSend Send(MousePacket[] packets) {
            Calls.Add(packets);
            if (OnSend!=null) OnSend();
            if (ThrowOnSend || (packets.Length==1 && ThrowOnRelease)) throw new IOException("test input uncertainty");
            return new RawSend(packets.Length==1?1:Count,Count==3?0:5);
        }
    }
    internal sealed class TestEnvironment : IClickEnvironment {
        public readonly DateTimeOffset Origin=DateTimeOffset.Parse("2030-01-02T00:00:00Z");
        public double Mono,Shift;
        public int Disposals;
        public bool EscapeFlag,Held,ThrowWait,ThrowDispose,FreshInput;
        public string Guard;
        public Action<TestEnvironment> Paused;
        public Action AtGuard;
        public readonly TestMouse Mouse=new TestMouse();
        public DateTimeOffset Now() { return Origin.AddTicks((long)((Mono+Shift)*10000)); }
        public double Monotonic { get { return Mono; } }
        public uint CurrentTick { get { return unchecked((uint)(1000000+Mono)); } }
        public uint LastInput { get { return FreshInput?CurrentTick:0; } }
        public bool Escape { get { return EscapeFlag; } }
        public bool ButtonsHeld { get { return Held; } }
        public bool HighResolution { get { return false; } }
        public string ValidateTarget(ClickPlan plan) { if (AtGuard!=null) AtGuard(); return Guard; }
        public void Pause(double ms,WaitHandle cancel) { if (ThrowWait) throw new IOException("test wait failure"); Mono+=ms<=0?0.25:ms; if (Paused!=null) Paused(this); }
        public void Dispatch(ClickPlan plan,ClickResult result,Func<bool> tryCommit) { InputDispatcher.Dispatch(plan,result,Now,Mouse,tryCommit); }
        public void Dispose() { Disposals++; if (ThrowDispose) throw new IOException("test cleanup failure"); }
    }
    public static class TimingFaults {
        private static List<object> cases;
        private static int assertions;
        private static void Check(bool condition,string message) { assertions++; if (!condition) throw new Exception(message); }
        private static void Case(string name,Action body) { try { body(); cases.Add(new {name=name,passed=true,error=(string)null}); } catch (Exception ex) { cases.Add(new {name=name,passed=false,error=ex.GetType().Name+": "+ex.Message}); } }
        private static bool Throws(Action body) { try { body(); return false; } catch { return true; } }
        private static TargetSnapshot Snapshot() { return new TargetSnapshot {Point=new DesktopInput.Point {X=200,Y=200},Root=new IntPtr(11),Leaf=new IntPtr(12),ProcessId=42,ThreadId=43,RootThreadId=44,ProcessCreated=1,Dpi=96,RootBounds=new Rectangle(0,0,800,600),LeafBounds=new Rectangle(100,100,400,300),ClientBounds=new Rectangle(0,0,800,600),DesktopBounds=new Rectangle(0,0,1920,1080),DisplayLayout="test-layout",DesktopName="test-desktop",ProcessName="test-only",QuietMilliseconds=550}; }
        private static ClickPlan Plan(TestEnvironment e,Func<bool> healthy=null) { return new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100,healthy); }
        private static ClickResult Done(ClickSchedule schedule) { Stopwatch t=Stopwatch.StartNew(); while (schedule.CompletedResult==null && t.ElapsedMilliseconds<3000) Thread.Sleep(1); Check(schedule.CompletedResult!=null,"scheduler did not finish"); return schedule.CompletedResult; }
        private static ClickResult Run(TestEnvironment e,Func<bool> healthy=null) { using (ClickSchedule s=new ClickSchedule(Plan(e,healthy),null,e,new TestLease(),true)) return Done(s); }
        public static string RunAll() {
            cases=new List<object>(); assertions=0;
            Case("NaN and infinity are rejected",delegate {
                foreach(double invalid in new double[]{Double.NaN,Double.PositiveInfinity,Double.NegativeInfinity,0,5001}) Check(Throws(delegate { new ClickPlan(DateTimeOffset.UtcNow,Snapshot(),invalid); }),"invalid lateness accepted");
                Check(ClickPlan.ValidateTime(DateTimeOffset.UtcNow,DateTimeOffset.UtcNow,100,Double.NaN,1)=="invalid_timing","NaN clock delta accepted");
            });
            Case("Snapshot cannot be changed after arming",delegate {
                TargetSnapshot original=Snapshot(); ClickPlan p=new ClickPlan(DateTimeOffset.UtcNow,original,100); original.Root=new IntPtr(99); p.Snapshot.Leaf=new IntPtr(99);
                Check(p.RootWindow==new IntPtr(11) && p.Snapshot.Leaf==new IntPtr(12),"plan snapshot remained mutable");
            });
            Case("Every identity and geometry guard detects a change",delegate {
                TargetSnapshot a=Snapshot();
                Action<TargetSnapshot>[] mutations={delegate(TargetSnapshot s){s.Root=new IntPtr(99);},delegate(TargetSnapshot s){s.Leaf=new IntPtr(99);},delegate(TargetSnapshot s){s.ProcessId++;},delegate(TargetSnapshot s){s.ThreadId++;},delegate(TargetSnapshot s){s.RootThreadId++;},delegate(TargetSnapshot s){s.ProcessCreated++;},delegate(TargetSnapshot s){s.RootBounds=new Rectangle(1,0,800,600);},delegate(TargetSnapshot s){s.LeafBounds=new Rectangle(101,100,400,300);},delegate(TargetSnapshot s){s.ClientBounds=new Rectangle(1,0,800,600);},delegate(TargetSnapshot s){s.Dpi=144;},delegate(TargetSnapshot s){s.DisplayLayout="changed";},delegate(TargetSnapshot s){s.DesktopName="locked";},delegate(TargetSnapshot s){s.MouseButtonsSwapped=true;},delegate(TargetSnapshot s){s.QuietMilliseconds++;},delegate(TargetSnapshot s){s.Point=new DesktopInput.Point{X=201,Y=200};}};
                string[] reasons={"target_changed","target_changed","target_changed","target_changed","target_changed","target_changed","target_moved","target_moved","target_moved","desktop_changed","desktop_changed","desktop_inactive","mouse_settings_changed","mouse_settings_changed","point_changed"};
                for(int i=0;i<mutations.Length;i++){TargetSnapshot b=Snapshot();mutations[i](b);Check(a.Difference(b)==reasons[i],"snapshot mutation "+i+" returned the wrong reason");}
                Check(a.Difference(Snapshot())==null,"identical snapshot rejected");
            });
            Case("Absolute coordinate mapping roundtrips every tested pixel",delegate {
                foreach(int width in new int[]{1,2,1919,1920,2560,3840,7680,32768,65536}) for(int x=0;x<width;x++) { int normalized=DesktopInput.NormalizeAbsolute(x-1920,-1920,width); int inverse=(int)((long)normalized*width/65536); Check(inverse==x,"pixel mapped to another pixel"); }
                Check(Throws(delegate {DesktopInput.NormalizeAbsolute(0,0,65537);}),"unrepresentable desktop accepted");
            });
            Case("Jalali date is explicit and invalid dates are rejected",delegate {
                DateTimeOffset date; Check(ClockLogic.TryTarget("1405/08/03","08:45:00.125",out date) && date.Millisecond==125,"valid date lost precision");
                Check(!ClockLogic.TryTarget("1405/13/01","08:45:00.000",out date),"invalid month accepted");
                Check(!ClockLogic.TryTarget("1405/07/31","08:45:00.000",out date),"invalid day accepted");
                Check(!ClockLogic.TryTarget("1405/08/03","24:00:00.000",out date),"invalid hour accepted");
                Check(!ClockLogic.TryTarget("05/08/03","08:45:00.000",out date),"ambiguous year accepted");
                Check(ClockLogic.TryTarget("1403/12/30","00:00:00.000",out date),"leap day rejected");
                Check(!ClockLogic.TryTarget("1404/12/30","00:00:00.000",out date),"non-leap day accepted");
            });
            Case("One dispatch produces exactly move/down/up",delegate { TestEnvironment e=new TestEnvironment(); ClickResult r=Run(e); Check(r.Outcome=="queued" && e.Mouse.Calls.Count==1,"dispatch not once"); Check(e.Mouse.Calls[0].Length==3 && e.Mouse.Calls[0][1].Flags==2 && e.Mouse.Calls[0][2].Flags==4,"wrong input sequence"); Check(r.SendStarted>=r.Target,"early send"); });
            Case("All partial input counts remain one-shot",delegate {
                foreach(uint count in new uint[]{0,1,2,3,4}) { TestEnvironment e=new TestEnvironment(); e.Mouse.Count=count; ClickResult r=Run(e); Check(e.Mouse.Calls.Count==(count==2?2:1),"click retried"); if(count==2) Check(e.Mouse.Calls[1].Length==1 && e.Mouse.Calls[1][0].Flags==4,"cleanup was not release-only"); if(count!=3) Check(r.Outcome!="queued","partial input promoted to success"); if(count==4) Check(r.QueuedEvents==null,"impossible return count treated as valid"); }
            });
            Case("Thrown native input result is ambiguous and never retried",delegate { TestEnvironment e=new TestEnvironment(); e.Mouse.ThrowOnSend=true; ClickResult r=Run(e); Check(r.Outcome=="input_exception_ambiguous" && r.DispatchAttempted && r.QueuedEvents==null && e.Mouse.Calls.Count==1,"uncertainty was hidden"); });
            Case("Failed release cleanup is reported without retry",delegate { TestEnvironment e=new TestEnvironment(); e.Mouse.Count=2;e.Mouse.ThrowOnRelease=true;ClickResult r=Run(e);Check(r.Outcome=="input_failed_or_partial" && r.ReleaseEvents==null && r.CleanupError!=null && e.Mouse.Calls.Count==2,"cleanup uncertainty hidden"); });
            Case("Dispatcher refuses early and late timestamps",delegate {
                TestEnvironment e=new TestEnvironment(); ClickPlan p=Plan(e);
                foreach(DateTimeOffset time in new DateTimeOffset[]{p.Target.AddTicks(-1),p.Target.AddTicks(1000001)}) { TestMouse sink=new TestMouse(); ClickResult r=new ClickResult(); InputDispatcher.Dispatch(p,r,delegate{return time;},sink,delegate{return true;});Check(sink.Calls.Count==0 && !r.DispatchAttempted && r.SendStarted==null,"out-of-window input sent"); }
            });
            Case("Clock failure after send preserves the known input result",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=Plan(e);TestMouse sink=new TestMouse();ClickResult r=new ClickResult();int n=0;
                InputDispatcher.Dispatch(p,r,delegate {n++;if(sink.Calls.Count>0) throw new IOException("clock read after queued input");return p.Target;},sink,delegate{return true;});
                Check(r.Outcome=="queued" && r.QueuedEvents==3 && r.SendReturned==null && r.TimingError!=null,"known queued input was overwritten or missing time fabricated");
            });
            Case("Forward and backward clock jumps send no input",delegate {
                foreach(int direction in new int[]{-1,1}) {TestEnvironment e=new TestEnvironment();e.Paused=delegate(TestEnvironment v){if(v.Mono>=500)v.Shift=direction*1000;};ClickResult r=Run(e);Check(r.Outcome=="clock_change" && e.Mouse.Calls.Count==0,"clock jump dispatched");}
            });
            Case("Cumulative small clock adjustments are bounded",delegate {
                TestEnvironment e=new TestEnvironment();e.Paused=delegate(TestEnvironment v){v.Shift+=10;};ClickResult r=Run(e);Check(r.Outcome=="clock_change" && e.Mouse.Calls.Count==0,"cumulative change missed");
            });
            Case("Suspend or a long scheduling gap cancels",delegate { TestEnvironment e=new TestEnvironment();e.Paused=delegate(TestEnvironment v){if(v.Mono>=500)v.Mono+=4000;};ClickResult r=Run(e);Check(r.Outcome=="scheduler_gap" && e.Mouse.Calls.Count==0,"resume dispatched"); });
            Case("Unresponsive UI cancels",delegate { TestEnvironment e=new TestEnvironment();ClickResult r=Run(e,delegate{return false;});Check(r.Outcome=="ui_unresponsive" && e.Mouse.Calls.Count==0,"stale UI dispatched"); });
            Case("Every rejected target/session guard sends zero input",delegate {
                foreach(string guard in new string[]{"target_changed","target_moved","desktop_changed","desktop_inactive","session_inactive","target_identity_unknown","input_state_unknown","target_dpi_unknown","mouse_settings_changed"}) {TestEnvironment e=new TestEnvironment();e.Guard=guard;ClickResult r=Run(e);Check(r.Outcome==guard && r.QueuedEvents==0 && r.SendStarted==null && r.LatenessMs==null,"guard bypassed or unknown timing fabricated");}
            });
            Case("Held modifiers and fresh input cancel",delegate {TestEnvironment a=new TestEnvironment();a.Held=true;Check(Run(a).Outcome=="button_or_modifier_held" && a.Mouse.Calls.Count==0,"held button dispatched");TestEnvironment b=new TestEnvironment();b.FreshInput=true;Check(Run(b).Outcome=="recent_input" && b.Mouse.Calls.Count==0,"fresh input dispatched");});
            Case("A backwards input tick cannot fake a quiet period",delegate {InputActivity a=new InputActivity();a.Observe(1000,0);a.Observe(500,1000);Check(!a.Quiet(2000,1100,550),"decreasing timestamp bypassed observation window");Check(a.Quiet(2000,1600,550),"genuine quiet period rejected");});
            Case("Input tick rollover is handled",delegate {InputActivity a=new InputActivity();a.Observe(UInt32.MaxValue-100,0);Check(a.Quiet(600,1000,550),"tick rollover failed");});
            Case("Prepared schedule cannot dispatch before Start",delegate {TestEnvironment e=new TestEnvironment();TestLease l=new TestLease();using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,l,false)){Check(e.Mouse.Calls.Count==0 && s.IsPending,"prepare dispatched");Check(s.Cancel("escape"),"prepare cancellation rejected");Check(Done(s).Outcome=="escape" && e.Mouse.Calls.Count==0,"cancelled prepare dispatched");Check(Throws(delegate{s.Start();}),"cancelled prepare restarted");}Check(e.Disposals==1 && l.Disposals==1,"prepare resources leaked");});
            Case("Clock changes during preparation prevent Start",delegate {TestEnvironment e=new TestEnvironment();using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new TestLease(),false)){e.Shift=-1000;Check(Throws(delegate{s.Start();}),"clock jump during prepare accepted");Check(e.Mouse.Calls.Count==0,"prepare clock jump dispatched");}});
            Case("Starting twice never creates a second dispatch",delegate {TestEnvironment e=new TestEnvironment();using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new TestLease(),false)){s.Start();Check(Throws(delegate{s.Start();}),"second start accepted");Done(s);}Check(e.Mouse.Calls.Count==1,"second dispatch occurred");});
            Case("Cancellation wins during target validation",delegate {
                TestEnvironment e=new TestEnvironment();using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.AtGuard=delegate{entered.Set();if(!release.WaitOne(2000))throw new Exception("guard wait timeout");};
                    using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new TestLease(),true)){Check(entered.WaitOne(2000),"guard not reached");Check(s.Cancel("escape"),"cancel did not win before dispatch");release.Set();ClickResult r=Done(s);Check(r.Outcome=="escape" && e.Mouse.Calls.Count==0,"accepted cancellation dispatched");}
                }
            });
            Case("After dispatch commitment cancellation does not claim success",delegate {
                TestEnvironment e=new TestEnvironment();using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.Mouse.OnSend=delegate{entered.Set();if(!release.WaitOne(2000))throw new Exception("send wait timeout");};
                    using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new TestLease(),true)){Check(entered.WaitOne(2000),"send not reached");Check(!s.Cancel("escape"),"irreversible cancellation claimed success");release.Set();Check(Done(s).Outcome=="queued","known dispatch lost");}
                }
            });
            Case("Cancel/start race stress: accepted cancellation means no dispatch",delegate {
                for(int i=0;i<128;i++){TestEnvironment e=new TestEnvironment();using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new TestLease(),true)){bool accepted=s.Cancel("escape");ClickResult r=Done(s);Check(!accepted || !r.DispatchAttempted,"cancel race sent input");Check(e.Mouse.Calls.Count<=1,"duplicate input in race");}}
            });
            Case("Callback failure cannot crash the worker or lose its result",delegate {TestEnvironment e=new TestEnvironment();using(ClickSchedule s=new ClickSchedule(Plan(e),delegate{throw new IOException("test callback");},e,new TestLease(),true)){ClickResult r=Done(s);Stopwatch t=Stopwatch.StartNew();while(s.NotificationError==null && t.ElapsedMilliseconds<1000)Thread.Sleep(1);Check(r.Outcome=="queued" && s.NotificationError=="IOException","callback exception escaped or result lost");}});
            Case("Lease cleanup failure preserves known dispatch",delegate {TestEnvironment e=new TestEnvironment();TestLease l=new TestLease();l.ThrowOnDispose=true;using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,l,true)){ClickResult r=Done(s);Check(r.Outcome=="queued" && r.CleanupError!=null && r.CleanupError.Contains("IOException"),"cleanup failure lost known result");}Check(l.Disposals==1,"lease released twice");});
            Case("Escape state cancels before dispatch",delegate {TestEnvironment e=new TestEnvironment();e.EscapeFlag=true;ClickResult r=Run(e);Check(r.Outcome=="escape" && e.Mouse.Calls.Count==0,"Escape ignored");});
            Case("Waiter and disposal failures release the lease",delegate {TestEnvironment e=new TestEnvironment();e.ThrowWait=true;e.ThrowDispose=true;TestLease l=new TestLease();using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,l,true)){ClickResult r=Done(s);Check(r.Outcome=="error_IOException" && r.CleanupError!=null && r.CleanupError.Contains("IOException") && e.Mouse.Calls.Count==0,"wait failure unsafe");}Check(l.Disposals==1,"lease leaked after environment disposal failed");});
            Case("Constructor rejection releases every acquired resource",delegate {TestEnvironment e=new TestEnvironment();e.ThrowDispose=true;TestLease l=new TestLease();Check(Throws(delegate{new ClickSchedule(new ClickPlan(e.Origin,Snapshot(),100),null,e,l,true);}),"past schedule accepted");Check(e.Disposals==1 && l.Disposals==1,"constructor leaked resources");});
            Case("Plan semaphore prevents duplicates and releases once",delegate {string name="Local\\BrsTimingClock-test-"+Guid.NewGuid().ToString("N");PlanLease first=new PlanLease(name);Check(Throws(delegate{new PlanLease(name);}),"parallel plan accepted");first.Dispose();first.Dispose();using(PlanLease second=new PlanLease(name)){};});
            Case("GUI mutex refuses a second thread and recovers",delegate {
                string name="Local\\BrsTimingClock-test-gui-"+Guid.NewGuid().ToString("N");bool blocked=false;
                using(ApplicationLease first=new ApplicationLease(name,delegate{})){Thread other=new Thread(delegate(){try{using(ApplicationLease second=new ApplicationLease(name,delegate{})){};}catch(InvalidOperationException){blocked=true;}});other.Start();Check(other.Join(2000),"mutex test hung");Check(blocked,"second GUI lease accepted");}
                using(ApplicationLease next=new ApplicationLease(name,delegate{})){}
            });
            int failed=0;foreach(object row in cases){if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;}
            return TimingSafety.Json(new { scope="fault injection only; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),cases=cases,total=cases.Count,failed=failed,assertions=assertions,passed=failed==0 });
        }
    }
}
