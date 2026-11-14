// Additional 2.1.2 contracts. Synthetic queues; never calls SendInput.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace BrsManualClock.Tests {
    internal sealed class RestoreProbe : IClickEnvironment,IExecutionScopeEnvironment {
        internal readonly RereviewEnvironment Inner=new RereviewEnvironment();internal int Restores;
        public IDisposable BeginExecution(){return new WindowsExecutionLease(delegate(uint flags){Restores++;return Restores==2?0u:0x80000001u;});}
        public DateTimeOffset Now(){return Inner.Now();}public double Monotonic{get{return Inner.Monotonic;}}public uint CurrentTick{get{return Inner.CurrentTick;}}public uint LastInput{get{return 0;}}
        public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}public bool HighResolution{get{return true;}}
        public string ValidateTarget(ClickPlan p){return null;}public void Pause(double ms,WaitHandle cancel){Inner.Pause(ms,cancel);}
        public void Dispatch(ClickPlan p,ClickResult r,Func<bool> gate){Inner.Dispatch(p,r,gate);}public void Dispose(){}
    }
    public static class RereviewHardening {
        private sealed class RehearsalProbe : IClickEnvironment {
            internal readonly RereviewEnvironment Inner=new RereviewEnvironment();internal readonly ManualResetEvent Entered=new ManualResetEvent(false),Release=new ManualResetEvent(false);
            public DateTimeOffset Now(){return Inner.Now();}public double Monotonic{get{return Inner.Monotonic;}}public uint CurrentTick{get{return Inner.CurrentTick;}}public uint LastInput{get{return 0;}}
            public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}public bool HighResolution{get{return true;}}
            public string ValidateTarget(ClickPlan p){return null;}public void Pause(double ms,WaitHandle cancel){Inner.Pause(ms,cancel);}
            public void Dispatch(ClickPlan p,ClickResult r,Func<bool> gate){Inner.Dispatch(p,r,gate);Entered.Set();Release.WaitOne(4000);}public void Dispose(){}
        }
        private static readonly List<object> cases=new List<object>();
        private static void Check(bool value,string message){if(!value)throw new Exception(message);}
        private static void Case(string name,Action body){try{body();cases.Add(new{name=name,passed=true,error=(string)null});}catch(Exception ex){cases.Add(new{name=name,passed=false,error=ex.GetBaseException().Message});}}
        private static TargetSnapshot Snapshot(){return new TargetSnapshot{Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=550};}
        private static ClickPlan Plan(RereviewEnvironment e){return new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100);}
        private static ClickResult Done(ClickSchedule s){Stopwatch wait=Stopwatch.StartNew();while(s.CompletedResult==null&&wait.ElapsedMilliseconds<4000)Thread.Sleep(1);Check(s.CompletedResult!=null,"Fixture worker did not finish.");return s.CompletedResult;}
        private static bool Throws(Action f){try{f();return false;}catch{return true;}}
        private static TimeZoneInfo TestZone(){return TimeZoneInfo.CreateCustomTimeZone("fixture-zone",TimeSpan.Zero,"fixture","fixture","fixture DST",new[]{TimeZoneInfo.AdjustmentRule.CreateAdjustmentRule(new DateTime(2020,1,1),new DateTime(2040,12,31),TimeSpan.FromHours(1),TimeZoneInfo.TransitionTime.CreateFixedDateRule(new DateTime(1,1,1,2,0,0),3,10),TimeZoneInfo.TransitionTime.CreateFixedDateRule(new DateTime(1,1,1,2,0,0),11,10))});}
        public static string RunAll(string directory){
            cases.Clear();Directory.CreateDirectory(directory);
            Case("Atomic UI health tracks heartbeat, failure and generation",delegate{
                UiHealthState health=new UiHealthState();Check(health.IsHealthy(0),"New heartbeat unavailable.");health.Invalidate();Check(!health.IsHealthy(0)&&health.IsHealthy(1),"Generation transition was not checked.");
                health.PulseAt(Stopwatch.GetTimestamp()-Stopwatch.Frequency);Check(!health.IsHealthy(1),"Old heartbeat accepted.");health.Pulse();Check(health.IsHealthy(1),"Fresh heartbeat refused.");health.Fail();health.Pulse();Check(!health.IsHealthy(1),"Failure latch cleared by pulse.");
            });
            Case("Unavailable high-resolution timer retains its error and refuses native mode",delegate{
                using(PreciseWait timer=new PreciseWait(delegate{return IntPtr.Zero;},delegate{return 123;})){
                    Check(!timer.Available&&timer.CreationError==123,"Creation failure was hidden.");bool blocked=false;
                    try{timer.RequireHighResolution();}catch(TargetGuardException error){blocked=error.Reason=="timer_high_resolution_unavailable"&&error.NativeCode==123;}
                    Check(blocked,"Unavailable timer passed the production preflight.");
                }
            });
            Case("Oversized desktop is rejected during plan construction",delegate{
                TargetSnapshot a=Snapshot();a.DesktopBounds=new Rectangle(0,0,65537,100);Check(Throws(delegate{new ClickPlan(DateTimeOffset.UtcNow,a,100);}),"Unrepresentable width accepted.");
                a=Snapshot();a.DesktopBounds=new Rectangle(0,0,100,65537);Check(Throws(delegate{new ClickPlan(DateTimeOffset.UtcNow,a,100);}),"Unrepresentable height accepted.");
            });
            Case("Automatic next occurrence skips invalid local time without changing explicit dates",delegate{
                TimeZoneInfo zone=TestZone();DateTimeOffset target;
                Check(!ClockLogic.TryLocalTarget(new DateTime(2026,3,10,2,30,0),zone,out target),"Explicit DST-gap date accepted.");
                target=ClockLogic.NextTarget(DateTimeOffset.Parse("2026-03-10T00:00:00Z"),new TimeSpan(2,30,0),zone);
                Check(TimeZoneInfo.ConvertTime(target,zone).Day==11,"Next occurrence did not skip invalid day.");
            });
            Case("Picker placement avoids the point on ordinary and negative-coordinate screens",delegate{
                foreach(int origin in new[]{0,-1920}){Rectangle placed;Point point=new Point(origin+960,540);Check(ClockLogic.TryNonCoveringBounds(new Rectangle(origin+600,180,720,720),point,new[]{new Rectangle(origin,0,1920,1080)},out placed)&&!placed.Contains(point),"Picker restored over target.");}
                Rectangle impossible;Check(!ClockLogic.TryNonCoveringBounds(new Rectangle(0,0,500,300),new Point(200,150),new[]{new Rectangle(0,0,500,300)},out impossible),"Impossible non-covering placement was invented.");
            });
            Case("Plan lease always closes its handle and preserves both cleanup errors",delegate{
                int releases=0,closes=0;PlanLease lease=new PlanLease(delegate{releases++;throw new IOException("fixture");},delegate{closes++;throw new FormatException("fixture");});AggregateException both=null;
                try{lease.Dispose();}catch(AggregateException error){both=error;}lease.Dispose();
                Check(releases==1&&closes==1&&both!=null&&both.InnerExceptions.Count==2,"Release failure hid disposal or lost an error.");
                Check(TimingSafety.ErrorKinds(both).Contains("IOException")&&TimingSafety.ErrorKinds(both).Contains("FormatException"),"Cleanup serialization hides an inner failure.");
            });
            Case("Execution restore retry is exercised through the real scheduler finally",delegate{
                RestoreProbe e=new RestoreProbe();ClickPlan p=new ClickPlan(e.Inner.Origin.AddSeconds(2),Snapshot(),100);
                using(ClickSchedule s=new ClickSchedule(p,null,e,new RereviewLease(),true)){ClickResult r=Done(s);Check(e.Restores==3&&r.Outcome=="queued"&&r.CleanupError.Contains("execution_scope:InvalidOperationException"),"Scheduler did not retry or erased the initial failure.");}
            });
            Case("A cancelled gate can retain a labelled decision sample without claiming input",delegate{
                RereviewEnvironment e=new RereviewEnvironment();ClickPlan p=Plan(e);ClickResult r=new ClickResult();InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddMilliseconds(1);},e.Mouse,delegate{return false;});
                Check(r.Outcome=="cancelled"&&r.LatenessMs==1&&r.DispatchCheckAt.HasValue&&!r.CommitAccepted&&!r.DispatchAttempted&&r.SendStarted==null&&e.Mouse.Calls==0,"Decision sample was confused with a send.");
            });
            Case("Deadline rechecked after commitment can skip the queue",delegate{
                RereviewEnvironment e=new RereviewEnvironment();ClickPlan p=Plan(e);ClickResult r=new ClickResult();int reads=0;InputDispatcher.Dispatch(p,r,delegate{return ++reads==1?p.Target:p.Target.AddMilliseconds(101);},e.Mouse,delegate{return true;});
                Check(r.CommitAccepted&&!r.DispatchAttempted&&r.Outcome=="too_late_after_commit"&&e.Mouse.Calls==0&&r.NativeError==null,"Late post-commit sample reached the queue.");
            });
            Case("Small rollback during target capture resumes waiting without an early click",delegate{
                RereviewEnvironment e=new RereviewEnvironment();int guards=0;
                e.AtGuard=delegate{if(++guards==1)e.Shift-=0.5;};
                using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true)){
                    ClickResult r=Done(s);Check(r.Outcome=="queued"&&guards>=2&&e.Mouse.Calls==1&&r.SendStarted>=r.Target,"Valid wait transition was rejected or input was early.");
                }
            });
            Case("Heartbeat expiry inside target capture prevents dispatch",delegate{
                RereviewEnvironment e=new RereviewEnvironment();UiHealthState health=new UiHealthState();
                e.AtGuard=delegate{health.PulseAt(Stopwatch.GetTimestamp()-Stopwatch.Frequency);};
                ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100,null,false,health,health.Generation);
                using(ClickSchedule s=new ClickSchedule(p,null,e,new RereviewLease(),true)){
                    ClickResult r=Done(s);Check(r.Outcome=="ui_unresponsive"&&e.Mouse.Calls==0&&!r.CommitAccepted,"Heartbeat expiry inside capture was ignored.");
                }
            });
            Case("Queue samples and native samples have distinct honest bases",delegate{
                RereviewEnvironment e=new RereviewEnvironment();ClickPlan p=Plan(e);ClickResult r=new ClickResult();int reads=0;InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddMilliseconds(++reads);},e.Mouse,delegate{return true;});
                Check(r.DispatchCheckAt==p.Target.AddMilliseconds(1)&&r.SendStarted==p.Target.AddMilliseconds(2)&&r.SendReturned==p.Target.AddMilliseconds(3)&&r.DispatchFinishedAt==p.Target.AddMilliseconds(4),"Queue chronology reuses the gate sample or includes cleanup.");
                Check(r.NativeCallEntrySample==null&&r.NativeCallReturnSample==null&&r.NativeCallDurationTicks==null&&r.SendTimingBasis=="managed_queue_wrapper_sample","Native measurements were fabricated for a synthetic queue.");
            });
            Case("Historical timeout remains observable after current timeout clears",delegate{
                RereviewEnvironment e=new RereviewEnvironment();using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.AtGuard=delegate{entered.Set();release.WaitOne(4000);};ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true);
                    try{Check(entered.WaitOne(2000),"Guard absent.");s.Dispose();Check(s.DisposeTimedOut&&s.EverDisposeTimedOut,"Timeout not recorded.");}finally{release.Set();}
                    Done(s);s.Dispose();Check(!s.DisposeTimedOut&&s.EverDisposeTimedOut&&s.ShutdownError==null,"Current and historical timeout meanings overlap.");
                }
            });
            Case("Blocked completion is separate from result publication and worker shutdown",delegate{
                RereviewEnvironment e=new RereviewEnvironment();using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    ClickSchedule s=new ClickSchedule(Plan(e),delegate{entered.Set();release.WaitOne(3000);},e,new RereviewLease(),true);
                    try{Done(s);Check(entered.WaitOne(2000),"Notification absent.");s.Dispose();Check(s.NotificationPending&&!s.IsWorkerActive&&!s.DisposeTimedOut,"Notification blocks worker disposal or is hidden.");}finally{release.Set();s.Dispose();}
                }
            });
            Case("Cancellation during rehearsal observation guarantees no input without rewriting the observation",delegate{
                RehearsalProbe e=new RehearsalProbe();ClickPlan p=new ClickPlan(e.Inner.Origin.AddSeconds(2),Snapshot(),100,null,true);
                ClickSchedule s=new ClickSchedule(p,null,e,new RereviewLease(),true);
                try{Check(e.Entered.WaitOne(2000)&&s.Phase=="rehearsal_observing","Rehearsal observation barrier absent.");Check(s.Cancel("escape")&&e.Inner.Mouse.Calls==0,"Rehearsal cancellation failed no-input contract.");}
                finally{e.Release.Set();Done(s);s.Dispose();e.Entered.Dispose();e.Release.Dispose();}
                Check(s.CompletedResult.Outcome=="rehearsal_due"&&!s.CompletedResult.DispatchAttempted,"Completed rehearsal was rewritten as an input cancellation.");
            });
            Case("Concurrent diagnostic appends preserve all messages",delegate{
                ClickResult r=new ClickResult();List<Thread> threads=new List<Thread>();for(int i=0;i<32;i++){int id=i;Thread t=new Thread(delegate(){r.AddCleanup("fixture-"+id);r.AddTiming("fixture-"+id);});threads.Add(t);t.Start();}foreach(Thread t in threads)Check(t.Join(2000),"Diagnostic append hung.");Check(r.CleanupError.Split(';').Length==32&&r.TimingError.Split(';').Length==32,"Concurrent append was lost.");
            });
            Case("Shutdown after commitment records unresolved input without inventing zero",delegate{
                RereviewEnvironment e=new RereviewEnvironment();string workspace=Path.Combine(directory,"unresolved-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(workspace);
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false))using(ClockWindow w=new ClockWindow(workspace,false)){
                    e.Mouse.OnCall=delegate(int n){entered.Set();release.WaitOne(4000);};ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true);
                    try{Check(entered.WaitOne(2000),"Dispatch absent.");typeof(ClockWindow).GetField("clickSchedule",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(w,s);typeof(ClockWindow).GetMethod("HandleClosed",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(w,new object[]{w,new FormClosedEventArgs(CloseReason.UserClosing)});
                        string log=File.ReadAllText(Directory.GetFiles(Path.Combine(workspace,"data","private","manual-clock"),"*.jsonl")[0]);Check(log.Contains("\"outcome_unresolved\":true")&&log.Contains("\"phase\":\"dispatching\"")&&log.Contains("\"native_input_count\":null"),"Committed shutdown ambiguity was hidden.");
                    }finally{release.Set();Done(s);s.Dispose();}
                }
            });
            int failed=0;foreach(object row in cases)if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;
            return TimingSafety.Json(new{scope="2.1.2 hardening; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=cases.Count,failed=failed,passed=failed==0,cases=cases});
        }
    }
}
