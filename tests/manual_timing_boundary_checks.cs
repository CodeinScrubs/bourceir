// Owned native-boundary contract simulations. Never invokes Windows SendInput.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Windows.Forms;

namespace BrsManualClock.Tests {
    internal sealed class BoundaryFixture : INativeBoundary {
        internal DateTimeOffset Target=DateTimeOffset.Parse("2030-01-02T00:00:02Z");
        internal int Reads,Calls,SafetyCalls;internal int ThrowRead;internal bool ThrowInvoke;
        internal string Reject;internal double EntryShift;internal uint Count=3;internal Action AtSafety;
        public DateTimeOffset Now(){Reads++;if(Reads==ThrowRead)throw new IOException("owned clock fault");return Target.AddTicks((long)((Reads==2?EntryShift:0)*10000));}
        public double Monotonic{get{return Reads>=2?Math.Max(0,EntryShift):0;}}public uint LastInput{get{return 0;}}
        public string Safety(ClickPlan plan){SafetyCalls++;if(AtSafety!=null)AtSafety();return Reject;}
        public RawSend Invoke(){Calls++;if(ThrowInvoke)throw new IOException("owned native-boundary fault");return new RawSend(Count,0);}
    }
    internal sealed class PreparedBoundaryFixture : IMouseQueue,IPreparedMouseQueue {
        internal BoundaryFixture Boundary;internal ClickPlan Plan;internal int Releases;
        public RawSend Click(){return BoundaryChecks.Execute(Plan,Boundary);}
        public RawSend Release(){Releases++;return new RawSend(1,0);}
        public RawSend Send(MousePacket[] packets){throw new Exception("prepared fixture should not allocate a generic send");}
    }
    internal sealed class CadenceEnvironment : IClickEnvironment {
        internal readonly RereviewEnvironment Inner=new RereviewEnvironment();internal readonly List<double> Far=new List<double>(),Near=new List<double>();
        public DateTimeOffset Now(){return Inner.Now();}public double Monotonic{get{return Inner.Monotonic;}}public uint CurrentTick{get{return Inner.CurrentTick;}}public uint LastInput{get{return 0;}}
        public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}public bool HighResolution{get{return true;}}
        public string ValidateTarget(ClickPlan p){return null;}public void Pause(double ms,WaitHandle cancel){if(Inner.Mono<5000)Far.Add(ms);else Near.Add(ms);Inner.Pause(ms,cancel);}
        public void Dispatch(ClickPlan p,ClickResult r,Func<bool> commit){Inner.Dispatch(p,r,commit);}public void Dispose(){}
    }
    internal sealed class CancelDuringSampleEnvironment : IClickEnvironment {
        internal readonly RereviewEnvironment Inner=new RereviewEnvironment();internal Action AtFirstWorkerSample;private int reads;
        public DateTimeOffset Now(){if(++reads==3&&AtFirstWorkerSample!=null)AtFirstWorkerSample();return Inner.Now();}
        public double Monotonic{get{return Inner.Monotonic;}}public uint CurrentTick{get{return Inner.CurrentTick;}}public uint LastInput{get{return 0;}}
        public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}public bool HighResolution{get{return true;}}
        public string ValidateTarget(ClickPlan p){return null;}public void Pause(double ms,WaitHandle cancel){Inner.Pause(ms,cancel);}
        public void Dispatch(ClickPlan p,ClickResult r,Func<bool> commit){Inner.Dispatch(p,r,commit);}public void Dispose(){}
    }
    internal sealed class CommittedEnvironment : IClickEnvironment,IMouseQueue,ICommittedMouseQueue,INativeBoundary {
        internal readonly DateTimeOffset Origin=DateTimeOffset.Parse("2030-01-02T00:00:00Z");
        internal double Mono,Shift;internal uint Last=0;internal int Calls,Queues,Releases;internal DispatchCommit Ticket;internal Action BeforeDispatch,BeforeBoundary,AtSafety;
        public DateTimeOffset Now(){return Origin.AddTicks((long)((Mono+Shift)*10000));}public double Monotonic{get{return Mono;}}
        public uint CurrentTick{get{return (uint)(1000000+Mono);}}public uint LastInput{get{return Last;}}
        public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}public bool HighResolution{get{return true;}}
        public string ValidateTarget(ClickPlan p){return null;}public void Pause(double ms,WaitHandle cancel){Mono+=ms<=0?0.25:ms;}
        public void Dispatch(ClickPlan p,ClickResult r,Func<bool> gate){if(BeforeDispatch!=null)BeforeDispatch();InputDispatcher.Dispatch(p,r,Now,this,gate);}public void Dispose(){}
        public RawSend Click(DispatchCommit ticket){Queues++;Ticket=ticket;if(BeforeBoundary!=null)BeforeBoundary();return NativeBoundaryDispatch.Execute(ticket.Plan,this,ticket);}
        public RawSend Release(){Releases++;return new RawSend(1,0);}public RawSend Send(MousePacket[] p){throw new Exception("Committed queue must use its ticket.");}
        public string Safety(ClickPlan p){if(AtSafety!=null)AtSafety();return null;}public RawSend Invoke(){Calls++;return new RawSend(3,0);}
    }
    public static class BoundaryChecks {
        internal static RawSend Execute(ClickPlan p,INativeBoundary b){return NativeBoundaryDispatch.Execute(p,b,p==null?null:new DispatchCommit(p,p.Target.AddSeconds(-2),-2000,p.Target,0,0));}
        private static readonly List<object> rows=new List<object>();
        private static void Check(bool value,string error){if(!value)throw new Exception(error);}
        private static void Case(string name,Action action){try{action();rows.Add(new{name=name,passed=true,error=(string)null});}catch(Exception ex){rows.Add(new{name=name,passed=false,error=ex.GetBaseException().Message});}}
        private static TargetSnapshot Snapshot(){return new TargetSnapshot{Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=550};}
        private static ClickPlan Plan(BoundaryFixture boundary,UiHealthState health=null){return new ClickPlan(boundary.Target,Snapshot(),100,null,false,health,health==null?0:health.Generation);}
        [MethodImpl(MethodImplOptions.NoInlining)]private static void ThrowRelease(){throw new IOException("owned release fault");}
        public static string RunAll(string directory){
            rows.Clear();Directory.CreateDirectory(directory);
            Case("Native boundary samples and invocation are distinct from commitment",delegate{
                BoundaryFixture b=new BoundaryFixture();RawSend r=Execute(Plan(b),b);
                Check(b.Calls==1&&b.SafetyCalls==1&&r.NativeBoundaryKnown&&r.NativeInvoked&&r.Count==3&&r.PreNativeCheckAt.HasValue&&r.NativeEntrySample.HasValue&&r.NativeReturnSample.HasValue,"Boundary did not preserve its known result.");
            });
            Case("Both pre-native clock failures are known no-input outcomes",delegate{
                foreach(int read in new[]{1,2}){
                    BoundaryFixture b=new BoundaryFixture{ThrowRead=read};RawSend r=Execute(Plan(b),b);Check(b.Calls==0&&!r.NativeInvoked&&r.NativeBoundaryKnown&&r.Count==0&&r.SkipReason=="native_preflight_failed"&&r.TimingError=="native_preflight:IOException","Preflight exception became ambiguous input.");
                    b=new BoundaryFixture{ThrowRead=read};ClickPlan p=Plan(b);ClickResult result=new ClickResult();InputDispatcher.Dispatch(p,result,delegate{return b.Target;},new PreparedBoundaryFixture{Boundary=b,Plan=p},delegate{return true;});
                    Check(result.NativeCallInvoked==false&&result.NativeError==null&&(read!=1||result.PreNativeCheckAt==null),"A failed native sample was replaced by a queue timestamp.");
                }
            });
            Case("Every final safety rejection preserves the committed-but-skipped contract",delegate{
                foreach(string reason in new[]{"target_changed","target_moved","desktop_inactive","input_interference_after_commit","escape_after_commit","recent_input_after_commit"}){
                    BoundaryFixture b=new BoundaryFixture{Reject=reason};ClickPlan p=Plan(b);PreparedBoundaryFixture queue=new PreparedBoundaryFixture{Boundary=b,Plan=p};ClickResult r=new ClickResult();
                    InputDispatcher.Dispatch(p,r,delegate{return b.Target;},queue,delegate{return true;});
                    Check(r.CommitAccepted&&!r.DispatchAttempted&&r.NativeCallInvoked==false&&r.QueuedEvents==0&&r.NativeError==null&&r.Outcome==reason&&b.Calls==0&&queue.Releases==0,"A final rejection sent input or claimed undone commitment.");
                }
            });
            Case("Health changing inside final native safety checks skips input",delegate{
                BoundaryFixture b=new BoundaryFixture();UiHealthState health=new UiHealthState();b.AtSafety=health.Fail;RawSend r=Execute(Plan(b,health),b);Check(b.Calls==0&&r.SkipReason=="ui_unresponsive_after_commit"&&!r.NativeInvoked,"Late failure latch was ignored.");
            });
            Case("Deadline change after final native safety checks skips input",delegate{
                foreach(double shift in new[]{-0.1,100.0001}){BoundaryFixture b=new BoundaryFixture{EntryShift=shift};RawSend r=Execute(Plan(b),b);Check(b.Calls==0&&!r.NativeInvoked&&r.SkipReason==(shift<0?"target_not_reached_after_commit":"too_late_after_commit"),"Native-boundary deadline was bypassed.");}
            });
            Case("Native-call exception remains ambiguous and is never retried",delegate{
                BoundaryFixture b=new BoundaryFixture{ThrowInvoke=true};ClickPlan p=Plan(b);PreparedBoundaryFixture queue=new PreparedBoundaryFixture{Boundary=b,Plan=p};ClickResult r=new ClickResult();
                InputDispatcher.Dispatch(p,r,delegate{return b.Target;},queue,delegate{return true;});
                Check(b.Calls==1&&queue.Releases==0&&r.Outcome=="input_exception_ambiguous"&&r.DispatchAttempted&&r.QueuedEvents==null&&r.NativeCallInvoked==null&&r.PreNativeCheckAt.HasValue&&r.NativeCallEntrySample.HasValue,"An unknown native count was guessed/retried or valid preflight evidence was lost.");
            });
            Case("Post-native clock failure preserves the actual return count and entry sample",delegate{
                BoundaryFixture b=new BoundaryFixture{ThrowRead=3};RawSend r=Execute(Plan(b),b);Check(b.Calls==1&&r.NativeInvoked&&r.Count==3&&r.NativeEntrySample.HasValue&&!r.NativeReturnSample.HasValue&&r.TimingError=="native_return:IOException","A known result or valid entry sample was erased.");
            });
            Case("Required release cleanup still runs if its optional entry clock fails",delegate{
                BoundaryFixture b=new BoundaryFixture{ThrowRead=1,Count=1,Reject="fixture must not run this check"};RawSend r=NativeBoundaryDispatch.Execute(null,b);Check(b.Calls==1&&b.SafetyCalls==0&&r.Count==1&&r.NativeInvoked&&!r.NativeEntrySample.HasValue&&r.TimingError=="release_entry:IOException","Clock/target guards prevented required release-only cleanup.");
            });
            Case("Callback pending state is visible before result publication",delegate{
                RereviewEnvironment env=new RereviewEnvironment();using(ManualResetEvent block=new ManualResetEvent(false)){
                    ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100),delegate{block.WaitOne(4000);},env,new RereviewLease(),true);
                    try{Stopwatch wait=Stopwatch.StartNew();while(s.CompletedResult==null&&wait.ElapsedMilliseconds<2000)Thread.Sleep(1);Check(s.CompletedResult!=null&&s.CompletionCallbackPending&&s.NotificationPending,"Published result briefly reported no callback pending.");}finally{block.Set();s.Dispose();}
                }
            });
            Case("Far waits use bounded slower cadence while final waits keep the existing cadence",delegate{
                CadenceEnvironment env=new CadenceEnvironment();using(ClickSchedule s=new ClickSchedule(new ClickPlan(env.Inner.Origin.AddSeconds(10),Snapshot(),100),null,env,new RereviewLease(),true)){
                    Stopwatch wait=Stopwatch.StartNew();while(s.CompletedResult==null&&wait.ElapsedMilliseconds<3000)Thread.Sleep(1);Check(s.CompletedResult!=null&&s.CompletedResult.Outcome=="queued","Cadence simulation did not finish.");
                    Check(env.Far.Count>0&&env.Near.Count>0&&env.Far.Contains(100)&&env.Near.Contains(50),"Wait cadence was not adapted.");foreach(double delay in env.Far)Check(delay<=100,"Far pause exceeds continuity budget.");foreach(double delay in env.Near)Check(delay<=50,"Near pause changed unexpectedly.");
                }
            });
            Case("Single PlanLease cleanup error preserves its originating stack",delegate{
                string trace=null;PlanLease lease=new PlanLease(ThrowRelease,delegate{});try{lease.Dispose();}catch(IOException error){trace=error.StackTrace;}Check(trace!=null&&trace.Contains("ThrowRelease"),"Cleanup rethrow reset the original stack.");
            });
            Case("Callback-only health remains an explicitly pre-capture compatibility hook",delegate{
                RereviewEnvironment env=new RereviewEnvironment();int callbacks=0;bool healthy=true;env.AtGuard=delegate{healthy=false;};
                using(ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100,delegate{callbacks++;return healthy;}),null,env,new RereviewLease(),true)){
                    Stopwatch wait=Stopwatch.StartNew();while(s.CompletedResult==null&&wait.ElapsedMilliseconds<2000)Thread.Sleep(1);Check(s.CompletedResult!=null&&s.CompletedResult.Outcome=="queued"&&callbacks>0,"Compatibility callback ran after capture; use atomic state for the stronger contract.");
                }
            });
            Case("Accepted lifecycle cancellation retains its reason when health invalidation races it",delegate{
                CancelDuringSampleEnvironment env=new CancelDuringSampleEnvironment();UiHealthState health=new UiHealthState();bool accepted=false;
                ClickPlan p=new ClickPlan(env.Inner.Origin.AddSeconds(2),Snapshot(),100,null,false,health,health.Generation);
                using(ClickSchedule s=new ClickSchedule(p,null,env,new RereviewLease(),false)){
                    env.AtFirstWorkerSample=delegate{accepted=s.Cancel("power_changed");health.Invalidate();};s.Start();
                    Stopwatch wait=Stopwatch.StartNew();while(s.CompletedResult==null&&wait.ElapsedMilliseconds<2000)Thread.Sleep(1);
                    Check(accepted&&s.CompletedResult!=null&&s.CompletedResult.Outcome=="power_changed"&&env.Inner.Mouse.Calls==0,"Health rejection overwrote the accepted cancellation reason.");
                }
            });
            Case("Real scheduler commitment carries its immutable timing and input anchors to the owned queue",delegate{
                CommittedEnvironment env=new CommittedEnvironment();ClickPlan p=new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),500);
                using(ClickSchedule s=new ClickSchedule(p,null,env,new RereviewLease(),true)){
                    Stopwatch w=Stopwatch.StartNew();while(s.CompletedResult==null&&w.ElapsedMilliseconds<2000)Thread.Sleep(1);ClickResult r=s.CompletedResult;
                    Check(r!=null&&r.Outcome=="queued"&&r.CommitAccepted&&r.NativeCallInvoked==true&&env.Calls==1&&env.Queues==1&&env.Ticket!=null&&Object.ReferenceEquals(env.Ticket.Plan,p)&&env.Ticket.OriginWall==env.Origin&&env.Ticket.OriginMono==0&&env.Ticket.AcceptedLastInput==0,"Ticket was missing, mixed or lost.");
                }
            });
            Case("A stall after the real commitment CAS is rejected by the native boundary",delegate{
                CommittedEnvironment env=new CommittedEnvironment();env.BeforeBoundary=delegate{env.Mono+=300;};
                using(ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),500),null,env,new RereviewLease(),true)){
                    Stopwatch w=Stopwatch.StartNew();while(s.CompletedResult==null&&w.ElapsedMilliseconds<2000)Thread.Sleep(1);ClickResult r=s.CompletedResult;
                    Check(r!=null&&r.CommitAccepted&&r.Outcome=="scheduler_gap_after_commit"&&!r.DispatchAttempted&&r.NativeCallInvoked==false&&r.QueuedEvents==0&&env.Queues==1&&env.Calls==0,"A true post-CAS gap reached invocation.");
                }
            });
            Case("Cumulative clock corrections remain bounded across commitment and final safety",delegate{
                CommittedEnvironment env=new CommittedEnvironment();env.BeforeDispatch=delegate{env.Shift+=80;};env.AtSafety=delegate{env.Shift+=40;};
                using(ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),500),null,env,new RereviewLease(),true)){
                    Stopwatch w=Stopwatch.StartNew();while(s.CompletedResult==null&&w.ElapsedMilliseconds<2000)Thread.Sleep(1);ClickResult r=s.CompletedResult;
                    Check(r!=null&&r.CommitAccepted&&r.Outcome=="clock_change_after_commit"&&env.Calls==0,"A new commit anchor forgot original cumulative drift.");
                }
            });
            Case("Missing or mismatched commit tickets cannot invoke the native boundary",delegate{
                BoundaryFixture b=new BoundaryFixture();ClickPlan p=Plan(b);RawSend a=NativeBoundaryDispatch.Execute(p,b);
                ClickPlan other=Plan(b);RawSend c=NativeBoundaryDispatch.Execute(p,b,new DispatchCommit(other,p.Target.AddSeconds(-2),-2000,p.Target,0,0));
                Check(b.Calls==0&&!a.NativeInvoked&&!c.NativeInvoked&&a.SkipReason=="commit_context_invalid"&&c.SkipReason=="commit_context_invalid","Unbound ticket was accepted.");
            });
            Case("Health invalidation publishes the lifecycle reason before cancellation can race it",delegate{
                UiHealthState health=new UiHealthState();long generation=health.Generation;health.InvalidateWithReason("power_changed");
                Check(!health.IsHealthy(generation)&&health.RejectionReason(generation)=="power_changed","Generation and lifecycle reason were torn.");
                BoundaryFixture b=new BoundaryFixture();ClickPlan p=new ClickPlan(b.Target,Snapshot(),100,null,false,health,generation);RawSend r=Execute(p,b);
                Check(!r.NativeInvoked&&r.SkipReason=="power_changed"&&b.Calls==0,"Late lifecycle invalidation lost its reason or sent input.");
            });
            Case("Result persistence retries only a known pre-write preparation failure once",delegate{
                using(ClockWindow window=new ClockWindow(Path.Combine(directory,"prewrite-retry"),false)){
                    TimingJournal journal=(TimingJournal)typeof(ClockWindow).GetField("journal",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(window);int reads=0;
                    typeof(TimingJournal).GetField("clock",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(journal,(Func<DateTimeOffset>)delegate{if(++reads==1)throw new IOException("owned preparation failure");return DesktopInput.PreciseUtcNow();});
                    ClickResult r=new ClickResult{ScheduleId="owned-prewrite-retry",Target=DateTimeOffset.Parse("2030-01-02T00:00:02Z"),Outcome="cancelled"};
                    MethodInfo record=typeof(ClockWindow).GetMethod("RecordResultOnce",BindingFlags.Instance|BindingFlags.NonPublic);
                    Check((bool)record.Invoke(window,new object[]{r,null})&&reads==2&&(bool)record.Invoke(window,new object[]{r,null})&&reads==2,"Known pre-write failure was lost or retry was unbounded.");
                }
            });
            Case("Failure cleanup continues after a disposed form makes hotkey cleanup throw",delegate{
                ClockWindow window=new ClockWindow(Path.Combine(directory,"cleanup-error"),false);TimingJournal journal=(TimingJournal)typeof(ClockWindow).GetField("journal",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(window);
                window.Dispose();typeof(ClockWindow).GetField("escapeReserved",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(window,true);
                window.FailClosed(new IOException("owned UI fault"));
                Check((bool)typeof(ClockWindow).GetField("failed",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(window),"Failure did not latch.");
                string[] markers=Directory.GetFiles(Path.GetDirectoryName(journal.PathName),"*.failed-*.json");bool found=false;foreach(string marker in markers){string json=File.ReadAllText(marker);if(json.Contains("application_error")&&json.Contains("ObjectDisposedException"))found=true;}
                Check(found,"Cleanup failure suppressed the best-effort application-error marker.");
            });
            int failed=0;foreach(object row in rows)if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;
            return TimingSafety.Json(new{scope="native-boundary simulations and owned resource contracts; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=rows.Count,failed=failed,passed=failed==0,cases=rows});
        }
    }
}
