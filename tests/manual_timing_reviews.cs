// External-review regressions. Simulated input only; never sends desktop input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;

namespace BrsManualClock.Tests {
    internal sealed class ReviewLease : IDisposable { public void Dispose() { throw new FormatException("fixture cleanup"); } }
    internal sealed class ReviewEnvironment : IClickEnvironment {
        internal readonly TestEnvironment Inner=new TestEnvironment();
        internal Action BeforeDispatch;
        public DateTimeOffset Now(){return Inner.Now();} public double Monotonic{get{return Inner.Monotonic;}}
        public uint CurrentTick{get{return Inner.CurrentTick;}}public uint LastInput{get{return Inner.LastInput;}}
        public bool Escape{get{return Inner.Escape;}}public bool ButtonsHeld{get{return Inner.ButtonsHeld;}}public bool HighResolution{get{return false;}}
        public string ValidateTarget(ClickPlan plan){return Inner.ValidateTarget(plan);}public void Pause(double ms,WaitHandle cancel){Inner.Pause(ms,cancel);}
        public void Dispatch(ClickPlan plan,ClickResult result,Func<bool> tryCommit){if(BeforeDispatch!=null)BeforeDispatch();InputDispatcher.Dispatch(plan,result,Now,Inner.Mouse,tryCommit);}
        public void Dispose(){Inner.Dispose();}
    }
    public static class ReviewChecks {
        private static readonly List<object> cases=new List<object>();
        private static int failed;
        private static void Require(bool value,string message) { if(!value)throw new Exception(message); }
        private static void Case(string name,Action body) { try{body();cases.Add(new{name=name,passed=true,error=(string)null});}catch(Exception e){failed++;cases.Add(new{name=name,passed=false,error=e.Message});} }
        private static TargetSnapshot Snapshot() { return new TargetSnapshot { Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=1 }; }
        private static ClickResult Done(ClickSchedule schedule) {
            Stopwatch limit=Stopwatch.StartNew();while(schedule.CompletedResult==null && limit.ElapsedMilliseconds<3000)Thread.Sleep(1);
            Require(schedule.CompletedResult!=null,"Schedule did not finish.");return schedule.CompletedResult;
        }
        private static ClickResult Run(TestEnvironment e,Func<bool> healthy=null,IDisposable lease=null) {
            using(ClickSchedule s=new ClickSchedule(new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100,healthy),null,e,lease??new TestLease(),true))return Done(s);
        }
        public static string RunAll() {
            cases.Clear();failed=0;
            Case("Target mutation by the final health callback is caught before input",delegate {
                TestEnvironment e=new TestEnvironment();int dueCallbacks=0;
                ClickResult r=Run(e,delegate{if(e.Mono>=2000 && ++dueCallbacks==2)e.Guard="target_changed";return true;});
                Require(r.Outcome=="target_changed" && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Stale target was dispatched after a health callback changed it.");
            });
            Case("Zero release count explicitly reports failed cleanup",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100);ClickResult r=new ClickResult();PreparedTestQueue q=new PreparedTestQueue{Count=2,ReleaseCount=0};
                InputDispatcher.Dispatch(p,r,delegate{return p.Target;},q,delegate{return true;});
                Require(q.Clicks==1 && q.Releases==1 && r.ReleaseEvents==0 && r.CleanupError=="release_not_queued","Failed release was not reported explicitly.");
            });
            Case("One tick early is distinct from late and never sends",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),1);ClickResult r=new ClickResult();
                InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddTicks(-1);},e.Mouse,delegate{return true;});
                Require(r.Outcome=="target_not_reached" && r.LatenessMs.HasValue && r.LatenessMs.Value<0 && e.Mouse.Calls.Count==0,"Early time was mislabeled or its measurement lost.");
            });
            Case("Rejected deadline retains its exact sample and signed lateness",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),1);ClickResult r=new ClickResult();DateTimeOffset sample=p.Target.AddTicks(10001);
                InputDispatcher.Dispatch(p,r,delegate{return sample;},e.Mouse,delegate{return true;});
                FieldInfo field=typeof(ClickResult).GetField("DispatchCheckAt");
                Require(r.Outcome=="too_late" && r.LatenessMs==1.0001 && field!=null && (DateTimeOffset?)field.GetValue(r)==sample && !r.DispatchAttempted,"Rejected timing evidence was lost.");
            });
            Case("Multiple cleanup failures are all retained",delegate {
                TestEnvironment e=new TestEnvironment{ThrowDispose=true};ClickResult r=Run(e,null,new ReviewLease());
                Require(r.Outcome=="queued" && r.CleanupError!=null && r.CleanupError.Contains("IOException") && r.CleanupError.Contains("FormatException"),"Cleanup failure overwrote earlier evidence.");
            });
            Case("An awake restore failure remains retryable on its owner thread",delegate {
                int calls=0;WindowsExecutionLease lease=new WindowsExecutionLease(delegate{calls++;return calls==2?0:0x80000000;});
                bool rejected=false;try{lease.Dispose();}catch(InvalidOperationException){rejected=true;}
                lease.Dispose();lease.Dispose();Require(rejected && calls==3,"A failed restore prevented a later successful restore.");
            });
            Case("Cancellation wins until the dispatcher's final timestamp gate",delegate {
                ReviewEnvironment e=new ReviewEnvironment();
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.BeforeDispatch=delegate{entered.Set();Require(release.WaitOne(2000),"Fixture barrier timed out.");};
                    using(ClickSchedule s=new ClickSchedule(new ClickPlan(e.Inner.Origin.AddSeconds(2),Snapshot(),100),null,e,new TestLease(),true)){
                        try{Require(entered.WaitOne(2000),"Final timestamp gate was not reached.");Require(s.Cancel("escape"),"Cancellation was rejected before the final timestamp gate.");}finally{release.Set();}
                        Require(Done(s).Outcome=="escape" && e.Inner.Mouse.Calls.Count==0,"Accepted final-gate cancellation dispatched input.");
                    }
                }
            });
            Case("Early and late timestamps never invoke the commit gate",delegate {
                foreach(long ticks in new long[]{-1,10001}){
                    TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),1);ClickResult r=new ClickResult();int commits=0;
                    InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddTicks(ticks);},e.Mouse,delegate{commits++;return true;});
                    Require(commits==0 && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Rejected time committed dispatch.");
                }
            });
            Case("Rehearsal commit never rejects cancellation as native input",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100,null,true);
                using(ClickSchedule s=new ClickSchedule(p,null,e,new TestLease(),true)){ClickResult r=Done(s);Require(s.Cancel() && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Rehearsal claimed native input commitment.");}
            });
            Case("Competing cancellers preserve the published winner reason",delegate {
                for(int i=0;i<128;i++){
                    TestEnvironment e=new TestEnvironment();using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                        e.AtGuard=delegate{entered.Set();Require(release.WaitOne(2000),"Guard fixture timed out.");};
                        using(ClickSchedule s=new ClickSchedule(new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100),null,e,new TestLease(),true)){
                            try{
                                Require(entered.WaitOne(2000),"Guard not reached.");
                                Thread a=new Thread(delegate(){s.Cancel("first");}),b=new Thread(delegate(){s.Cancel("second");});a.Start();b.Start();Require(a.Join(1000)&&b.Join(1000),"Canceller stalled.");
                            }finally{release.Set();}
                            ClickResult r=Done(s);Require((r.Outcome=="first" || r.Outcome=="second") && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Reason publication was stale or accepted cancel dispatched.");
                        }
                    }
                }
            });
            Case("Dispose timeout is visible and a later join releases resources once",delegate {
                TestEnvironment e=new TestEnvironment();TestLease lease=new TestLease();
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.AtGuard=delegate{entered.Set();Require(release.WaitOne(4000),"Guard fixture timed out.");};
                    ClickSchedule s=new ClickSchedule(new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100),null,e,lease,true);
                    try{
                        Require(entered.WaitOne(2000),"Guard not reached.");s.Dispose();
                        Require(s.DisposeTimedOut && s.ShutdownError=="worker_shutdown_timeout" && s.IsWorkerActive && e.Disposals==0 && lease.Disposals==0,"Running worker or shutdown timeout was hidden.");
                    }finally{release.Set();}
                    Done(s);s.Dispose();Require(!s.IsWorkerActive && lease.Disposals==1 && e.Disposals==1 && e.Mouse.Calls.Count==0,"Timed-out worker leaked resources or clicked.");
                }
            });
            Case("Native INPUT layout matches this process architecture",delegate {
                Require(DesktopInput.NativeInputSize==(IntPtr.Size==8?40:28) && DesktopInput.NativeMouseInputSize==(IntPtr.Size==8?32:24),"Native INPUT ABI mismatch.");
            });
            Case("Invalid waits reject before touching a timer or input",delegate {
                using(PreciseWait wait=new PreciseWait())using(ManualResetEvent cancel=new ManualResetEvent(true)){
                    foreach(double ms in new double[]{Double.NaN,Double.PositiveInfinity,Double.NegativeInfinity,-1,(double)Int32.MaxValue+1}){
                        bool rejected=false;try{wait.Pause(ms,cancel);}catch(ArgumentOutOfRangeException){rejected=true;}Require(rejected,"Invalid wait was accepted.");
                    }
                    wait.Pause(0,cancel);wait.Pause(1,cancel);wait.Pause(1,cancel);
                    Require(PreciseWait.HighResolutionFlag==2,"Wrong high-resolution timer flag.");
                }
            });
            Case("Timezone gaps and folds reject; ordinary Tehran time round-trips",delegate {
                TimeZoneInfo.TransitionTime start=TimeZoneInfo.TransitionTime.CreateFixedDateRule(new DateTime(1,1,1,2,0,0),3,1),end=TimeZoneInfo.TransitionTime.CreateFixedDateRule(new DateTime(1,1,1,2,0,0),10,1);
                TimeZoneInfo zone=TimeZoneInfo.CreateCustomTimeZone("test-only",TimeSpan.Zero,"test-only","standard","summer",new[]{TimeZoneInfo.AdjustmentRule.CreateAdjustmentRule(new DateTime(2030,1,1),new DateTime(2030,12,31),TimeSpan.FromHours(1),start,end)});
                DateTimeOffset result;
                Require(!ClockLogic.TryLocalTarget(new DateTime(2030,3,1,2,30,0),zone,out result),"Invalid local time was accepted.");
                Require(!ClockLogic.TryLocalTarget(new DateTime(2030,10,1,1,30,0),zone,out result),"Ambiguous local time was accepted.");
                Require(ClockLogic.TryLocalTarget(new DateTime(2026,10,24,8,45,0),ClockLogic.Tehran,out result) && result.Hour==8 && result.Offset==TimeSpan.FromMinutes(210),"Ordinary Tehran time changed.");
            });
            Case("Fresh input and held keys during the final target capture reject",delegate {
                TestEnvironment held=new TestEnvironment();held.AtGuard=delegate{held.Held=true;};Require(Run(held).Outcome=="button_or_modifier_held" && held.Mouse.Calls.Count==0,"Input after the health callback bypassed checks.");
                TestEnvironment fresh=new TestEnvironment();fresh.AtGuard=delegate{fresh.FreshInput=true;};Require(Run(fresh).Outcome=="recent_input" && fresh.Mouse.Calls.Count==0,"Fresh input during target capture bypassed checks.");
            });
            Case("Native window class changes reject even at unchanged geometry",delegate {
                TargetSnapshot a=Snapshot(),b=Snapshot();a.RootClass=b.RootClass="root";a.LeafClass="original";b.LeafClass="replacement";
                Require(a.Difference(b)=="target_changed","Changed native class was accepted.");
            });
            return TimingSafety.Json(new{scope="External-review regressions; simulated input; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),passed=failed==0,total=cases.Count,failed=failed,cases=cases});
        }
    }
}
