// Candidate-inspired refinements. Fakes and token queries only; no desktop input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;

namespace BrsManualClock.Tests {
    internal sealed class PreparedTestQueue : IMouseQueue, IPreparedMouseQueue {
        internal uint Count=3,ReleaseCount=1;
        internal int Clicks,Releases,GenericCalls;
        internal bool ThrowClick,ThrowRelease;
        public RawSend Send(MousePacket[] packets) { GenericCalls++;throw new Exception("Generic dispatch must not be used."); }
        public RawSend Click() { Clicks++;if(ThrowClick)throw new IOException("unknown click result");return new RawSend(Count,Count==3?0:5); }
        public RawSend Release() { Releases++;if(ThrowRelease)throw new IOException("unknown release result");return new RawSend(ReleaseCount,0); }
    }
    internal sealed class ScopeTestLease : IDisposable {
        private readonly Action finish;
        internal ScopeTestLease(Action action) {finish=action;}
        public void Dispose() {finish();}
    }
    internal sealed class ScopedTestEnvironment : IClickEnvironment, IExecutionScopeEnvironment {
        internal readonly TestEnvironment Inner=new TestEnvironment();
        internal int Begins,Ends,BeginThread,EndThread;
        internal bool BeginFails,EndFails;
        internal Action AtBegin;
        public IDisposable BeginExecution() {
            Begins++;BeginThread=Thread.CurrentThread.ManagedThreadId;
            if(AtBegin!=null)AtBegin();if(BeginFails)throw new IOException("scope acquire");
            return new ScopeTestLease(delegate{Ends++;EndThread=Thread.CurrentThread.ManagedThreadId;if(EndFails)throw new IOException("scope release");});
        }
        public DateTimeOffset Now() {return Inner.Now();}
        public double Monotonic {get{return Inner.Monotonic;}}
        public uint CurrentTick {get{return Inner.CurrentTick;}}
        public uint LastInput {get{return Inner.LastInput;}}
        public bool Escape {get{return Inner.Escape;}}
        public bool ButtonsHeld {get{return Inner.ButtonsHeld;}}
        public bool HighResolution {get{return false;}}
        public string ValidateTarget(ClickPlan plan) {return Inner.ValidateTarget(plan);}
        public void Pause(double ms,WaitHandle cancel) {Inner.Pause(ms,cancel);}
        public void Dispatch(ClickPlan plan,ClickResult result,Func<bool> tryCommit) {Inner.Dispatch(plan,result,tryCommit);}
        public void Dispose() {Inner.Dispose();}
    }
    public static class TimingRefinements {
        private static readonly List<object> cases=new List<object>();
        private static int failures;
        private static void Check(bool value,string message) {if(!value)throw new Exception(message);}
        private static void Case(string name,Action body) {try{body();cases.Add(new{name=name,passed=true,error=(string)null});}catch(Exception e){failures++;cases.Add(new{name=name,passed=false,error=e.GetType().Name+": "+e.Message});}}
        private static TargetSnapshot Snapshot() {return new TargetSnapshot {Point=new DesktopInput.Point{X=20,Y=30},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,1920,1080),QuietMilliseconds=1};}
        private static ClickPlan Plan(TestEnvironment environment,bool rehearsal=false) {return new ClickPlan(environment.Origin.AddSeconds(2),Snapshot(),100,null,rehearsal);}
        private static ClickResult Run(ClickPlan plan,IClickEnvironment environment) {
            using(ClickSchedule schedule=new ClickSchedule(plan,null,environment,new TestLease(),true)) {
                Stopwatch limit=Stopwatch.StartNew();while(schedule.CompletedResult==null && limit.ElapsedMilliseconds<3000)Thread.Sleep(1);
                Check(schedule.CompletedResult!=null,"Schedule did not finish.");return schedule.CompletedResult;
            }
        }
        public static string RunAll() {
            cases.Clear();failures=0;
            Case("Native batches are constructed without injecting input",delegate {
                TestEnvironment environment=new TestEnvironment();ClickPlan plan=Plan(environment);
                DesktopInput.NativeBatch native=new DesktopInput.NativeBatch(InputDispatcher.Packets(plan),plan);
                Check(native.Count==3,"Native move/down/up was not prepared.");
                new NativeMouseQueue(plan); // Constructor only; neither SendInput nor movement.
            });
            Case("Prepared dispatch bypasses the allocating generic sender",delegate {
                TestEnvironment environment=new TestEnvironment();ClickPlan plan=Plan(environment);PreparedTestQueue queue=new PreparedTestQueue();ClickResult result=new ClickResult();
                InputDispatcher.Dispatch(plan,result,delegate{return plan.Target;},queue,delegate{return true;});
                Check(queue.Clicks==1 && queue.GenericCalls==0 && result.Outcome=="queued","Prepared input path was bypassed.");
            });
            Case("Every prepared partial count stays one-shot with release-only cleanup",delegate {
                foreach(uint count in new uint[]{0,1,2,3,4}) {
                    TestEnvironment environment=new TestEnvironment();ClickPlan plan=Plan(environment);PreparedTestQueue queue=new PreparedTestQueue{Count=count};ClickResult result=new ClickResult();
                    InputDispatcher.Dispatch(plan,result,delegate{return plan.Target;},queue,delegate{return true;});
                    Check(queue.Clicks==1 && queue.GenericCalls==0 && queue.Releases==(count==2?1:0),"Prepared input was retried or incorrectly released.");
                    Check((count==3)==(result.Outcome=="queued"),"Partial count was treated as complete.");
                    if(count==4)Check(result.QueuedEvents==null,"Impossible count was fabricated as known.");
                }
            });
            Case("Prepared click and release exceptions preserve ambiguity",delegate {
                TestEnvironment environment=new TestEnvironment();ClickPlan plan=Plan(environment);PreparedTestQueue queue=new PreparedTestQueue{ThrowClick=true};ClickResult result=new ClickResult();
                InputDispatcher.Dispatch(plan,result,delegate{return plan.Target;},queue,delegate{return true;});
                Check(queue.Clicks==1 && result.QueuedEvents==null && result.Outcome=="input_exception_ambiguous","Unknown send was hidden or retried.");
                queue=new PreparedTestQueue{Count=2,ThrowRelease=true};result=new ClickResult();InputDispatcher.Dispatch(plan,result,delegate{return plan.Target;},queue,delegate{return true;});
                Check(queue.Clicks==1 && queue.Releases==1 && result.ReleaseEvents==null && result.CleanupError!=null,"Unknown release was hidden or retried.");
                queue=new PreparedTestQueue{Count=2,ReleaseCount=2};result=new ClickResult();InputDispatcher.Dispatch(plan,result,delegate{return plan.Target;},queue,delegate{return true;});
                Check(result.ReleaseEvents==null && result.CleanupError=="invalid_release_count","Impossible release count was treated as known.");
            });
            Case("Rehearsal reaches the actual scheduler deadline without input",delegate {
                TestEnvironment environment=new TestEnvironment();ClickResult result=Run(Plan(environment,true),environment);
                Check(result.Outcome=="rehearsal_due" && result.Rehearsal && result.RehearsalObservedAt>=result.Target,"Rehearsal did not observe its target.");
                Check(environment.Mouse.Calls.Count==0 && !result.DispatchAttempted && result.QueuedEvents==0 && result.SendStarted==null && result.SendReturned==null,"Rehearsal claimed or attempted native input.");
            });
            Case("Rehearsal retains deadline, target and input guards",delegate {
                foreach(string reason in new string[]{"target_changed","desktop_inactive","target_moved"}) {
                    TestEnvironment environment=new TestEnvironment{Guard=reason};ClickResult result=Run(Plan(environment,true),environment);
                    Check(result.Outcome==reason && !result.DispatchAttempted && environment.Mouse.Calls.Count==0,"Rehearsal bypassed target checks.");
                }
                TestEnvironment clock=new TestEnvironment();ClickPlan plan=Plan(clock,true);ClickResult late=new ClickResult();InputDispatcher.Dispatch(plan,late,delegate{return plan.Target.AddMilliseconds(101);},null,delegate{return true;});
                Check(late.Outcome=="too_late" && late.RehearsalObservedAt==null,"Rehearsal bypassed its deadline.");
            });
            Case("Awake lease restores its prior state exactly once",delegate {
                List<uint> flags=new List<uint>();WindowsExecutionLease lease=new WindowsExecutionLease(delegate(uint requested){flags.Add(requested);return 0x80000001;});
                lease.Dispose();lease.Dispose();Check(flags.Count==2 && flags[0]==0x80000003 && flags[1]==0x80000001,"Awake flags were not scoped/restored.");
            });
            Case("Awake acquire and restore failures are reported",delegate {
                bool failed=false;try{new WindowsExecutionLease(delegate{return 0;});}catch(InvalidOperationException){failed=true;}Check(failed,"Failed awake reservation was ignored.");
                int calls=0;WindowsExecutionLease lease=new WindowsExecutionLease(delegate{calls++;return calls==1?0x80000000:0;});
                failed=false;try{lease.Dispose();}catch(InvalidOperationException){failed=true;}Check(failed && calls==2,"Failed awake cleanup was hidden.");
            });
            Case("Awake restoration cannot run on an unrelated thread",delegate {
                int calls=0;bool rejected=false;WindowsExecutionLease lease=new WindowsExecutionLease(delegate{calls++;return 0x80000000;});
                Thread other=new Thread(delegate(){try{lease.Dispose();}catch(InvalidOperationException){rejected=true;}});other.Start();Check(other.Join(1000),"Cross-thread check stalled.");
                Check(rejected && calls==1,"Foreign thread changed execution state.");lease.Dispose();Check(calls==2,"Owner could not restore its state.");
            });
            Case("Execution scope is cleaned on completion and rejection",delegate {
                foreach(bool rejected in new bool[]{false,true}) {
                    ScopedTestEnvironment environment=new ScopedTestEnvironment();if(rejected)environment.Inner.Guard="target_changed";
                    ClickResult result=Run(Plan(environment.Inner),environment);
                    Check(environment.Begins==1 && environment.Ends==1 && environment.BeginThread==environment.EndThread,"Execution scope leaked or crossed threads.");
                    Check(result.Outcome==(rejected?"target_changed":"queued"),"Scope changed the scheduling result.");
                }
            });
            Case("Execution-scope failure never creates an unreported click",delegate {
                ScopedTestEnvironment acquire=new ScopedTestEnvironment{BeginFails=true};ClickResult first=Run(Plan(acquire.Inner),acquire);
                Check(!first.DispatchAttempted && acquire.Inner.Mouse.Calls.Count==0 && acquire.Ends==0,"Failed acquisition still dispatched.");
                ScopedTestEnvironment release=new ScopedTestEnvironment{EndFails=true};ClickResult second=Run(Plan(release.Inner),release);
                Check(second.Outcome=="queued" && second.CleanupError!=null && release.Ends==1,"Failed cleanup lost the known queued result.");
            });
            Case("Clock changes during execution-scope acquisition cancel",delegate {
                ScopedTestEnvironment environment=new ScopedTestEnvironment();environment.AtBegin=delegate{environment.Inner.Mono+=500;environment.Inner.Shift-=450;};
                ClickResult result=Run(Plan(environment.Inner),environment);
                Check(result.Outcome=="clock_change" && !result.DispatchAttempted && environment.Ends==1,"Setup hid a clock correction or leaked its lease.");
            });
            Case("Both root and leaf integrity levels are checked",delegate {
                foreach(uint current in new uint[]{0,0x1000,0x2000,0x3000,0x4000})foreach(uint root in new uint[]{0,0x1000,0x2000,0x3000,0x4000})foreach(uint leaf in new uint[]{0,0x1000,0x2000,0x3000,0x4000})
                    Check((InputPrivilege.Difference(current,leaf,root)==null)==(leaf<=current && root<=current),"Input privilege comparison was wrong.");
            });
            Case("Read-only Windows token query succeeds and unknown identity blocks",delegate {
                uint own=(uint)Process.GetCurrentProcess().Id;uint level=InputPrivilege.Read(own);Check(InputPrivilege.Difference(level,level,level)==null,"Own token level was rejected.");
                bool rejected=false;try{InputPrivilege.Read(UInt32.MaxValue);}catch(TargetGuardException e){rejected=e.Reason=="target_integrity_unknown";}Check(rejected,"Unknown process privilege was guessed.");
            });
            return TimingSafety.Json(new{scope="Refinement regressions; simulated input and power setters plus read-only token queries; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),passed=failures==0,total=cases.Count,failed=failures,cases=cases});
        }
    }
}
