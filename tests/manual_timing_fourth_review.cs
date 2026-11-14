// Fourth-round reproductions. Synthetic queues, unshown forms and owned files only.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace BrsManualClock.Tests {
    internal sealed class FourthBoundary : INativeBoundary {
        internal DateTimeOffset Target=DateTimeOffset.Parse("2030-01-02T00:00:02Z");
        internal double Wall,Mono;internal uint Last;internal int Calls;internal Action AtSafety;
        public DateTimeOffset Now(){return Target.AddMilliseconds(Wall);}
        public double Monotonic {get{return Mono;}}
        public uint LastInput {get{return Last;}}
        public string Safety(ClickPlan p){if(AtSafety!=null)AtSafety();return null;}
        public RawSend Invoke(){Calls++;return new RawSend(3,0);}
    }
    internal sealed class NullSkipMouse : IMouseQueue {
        public RawSend Send(MousePacket[] p){RawSend r=new RawSend(0,0);r.NativeInvoked=false;r.NativeBoundaryKnown=true;return r;}
    }
    public static class FourthReviewChecks {
        private static readonly List<object> rows=new List<object>();
        private const BindingFlags Instance=BindingFlags.Instance|BindingFlags.NonPublic;
        private static void Check(bool ok,string error){if(!ok)throw new Exception(error);}
        private static void Case(string name,Action run){try{run();rows.Add(new{name=name,passed=true,error=(string)null});}catch(Exception e){rows.Add(new{name=name,passed=false,error=e.GetBaseException().Message});}}
        private static TargetSnapshot Snapshot(){return new TargetSnapshot{Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=550};}
        private static ClickPlan Plan(FourthBoundary b,double late){return new ClickPlan(b.Target,Snapshot(),late);}
        private static RawSend Execute(ClickPlan p,FourthBoundary b){
            Type type=typeof(ClickPlan).Assembly.GetType("BrsManualClock.DispatchCommit");
            if(type==null)return NativeBoundaryDispatch.Execute(p,b);
            object ticket=Activator.CreateInstance(type,BindingFlags.Instance|BindingFlags.NonPublic,null,new object[]{p,b.Target.AddSeconds(-2),-2000.0,b.Target,0.0,(uint)0},null);
            return (RawSend)typeof(NativeBoundaryDispatch).GetMethod("Execute",BindingFlags.Static|BindingFlags.NonPublic,null,new Type[]{typeof(ClickPlan),typeof(INativeBoundary),type},null).Invoke(null,new object[]{p,b,ticket});
        }
        private static void Field(object owner,string name,object value){owner.GetType().GetField(name,Instance).SetValue(owner,value);}
        private static object Field(object owner,string name){return owner.GetType().GetField(name,Instance).GetValue(owner);}
        private static void Call(object owner,string name,params object[] args){owner.GetType().GetMethod(name,Instance).Invoke(owner,args);}
        public static string RunAll(string directory){
            rows.Clear();Directory.CreateDirectory(directory);
            Case("A 300 ms post-commit stall is rejected even with a 500 ms lateness budget",delegate{
                FourthBoundary b=new FourthBoundary{Wall=300,Mono=300};RawSend r=Execute(Plan(b,500),b);
                Check(b.Calls==0&&!r.NativeInvoked&&r.SkipReason=="scheduler_gap_after_commit","Wider lateness bypassed the independent gap policy.");
            });
            Case("A stall plus rollback inside native safety cannot hide elapsed time",delegate{
                FourthBoundary b=new FourthBoundary();b.AtSafety=delegate{b.Mono=400;b.Wall=50;};RawSend r=Execute(Plan(b,500),b);
                Check(b.Calls==0&&!r.NativeInvoked&&r.SkipReason=="clock_change_after_commit","400 ms elapsed plus rollback reached invocation.");
            });
            Case("Post-commit wall-clock steps are rejected independently of maximum lateness",delegate{
                FourthBoundary b=new FourthBoundary{Wall=150,Mono=0};RawSend r=Execute(Plan(b,5000),b);
                Check(b.Calls==0&&!r.NativeInvoked&&r.SkipReason=="clock_change_after_commit","Clock step was treated as ordinary lateness.");
            });
            Case("Any changed last-input tick after commitment is rejected including backwards ticks",delegate{
                FourthBoundary b=new FourthBoundary{Last=UInt32.MaxValue-10};RawSend r=Execute(Plan(b,500),b);
                Check(b.Calls==0&&!r.NativeInvoked&&r.SkipReason=="recent_input_after_commit","Changed input tick was lost after commitment.");
            });
            Case("All known pre-commit and committed queue deadline exits say native invocation false",delegate{
                foreach(int mode in new[]{0,1,2,3}){
                    FourthBoundary b=new FourthBoundary();ClickPlan p=Plan(b,100);ClickResult r=new ClickResult();int reads=0;
                    InputDispatcher.Dispatch(p,r,delegate{int n=++reads;return p.Target.AddMilliseconds(mode==0?-1:mode==1?101:n==2?(mode==2?-1:101):0);},new RereviewMouse(),delegate{return true;});
                    Check(r.NativeCallInvoked==false&&!r.DispatchAttempted&&r.QueuedEvents==0,"Known no-input exit has unknown native invocation.");
                }
            });
            Case("A queue-entry clock exception is a known no-input failure",delegate{
                FourthBoundary b=new FourthBoundary();ClickPlan p=Plan(b,100);ClickResult r=new ClickResult();RereviewMouse mouse=new RereviewMouse();int reads=0;
                try{InputDispatcher.Dispatch(p,r,delegate{if(++reads==2)throw new IOException("owned clock failure");return p.Target;},mouse,delegate{return true;});}catch(IOException){}
                Check(mouse.Calls==0&&!r.DispatchAttempted&&r.NativeCallInvoked==false&&r.QueuedEvents==0&&r.Outcome=="queue_preflight_failed"&&r.TimingError!=null,"Pre-queue failure became missing or ambiguous evidence.");
            });
            Case("Malformed internal skipped results still have a non-null outcome",delegate{
                FourthBoundary b=new FourthBoundary();ClickPlan p=Plan(b,100);ClickResult r=new ClickResult();InputDispatcher.Dispatch(p,r,delegate{return p.Target;},new NullSkipMouse(),delegate{return true;});
                Check(r.NativeCallInvoked==false&&r.Outcome=="native_call_skipped"&&r.QueuedEvents==0,"Skip produced a null outcome.");
            });
            Case("Pre-write serialization failure does not consume a journal sequence number",delegate{
                using(TimingJournal journal=new TimingJournal(Path.Combine(directory,"sequence"))){Dictionary<string,object> circular=new Dictionary<string,object>();circular["self"]=circular;bool failed=false;
                    try{journal.Append("bad",circular);}catch(Exception){failed=true;}journal.Append("good",new{ok=true});
                    string first;using(FileStream input=new FileStream(journal.PathName,FileMode.Open,FileAccess.Read,FileShare.ReadWrite))using(StreamReader reader=new StreamReader(input))first=reader.ReadToEnd();
                    Check(failed&&first.Contains("\"sequence\":1")&&!first.Contains("\"sequence\":2"),"A known pre-write failure left a sequence gap.");}
            });
            Case("Queued results with timing or cleanup faults are shown as faults",delegate{
                foreach(bool timing in new[]{true,false})using(ClockWindow w=new ClockWindow(Path.Combine(directory,"colour-"+timing),false)){
                    RereviewEnvironment env=new RereviewEnvironment();ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100),null,env,new RereviewLease(),false);Field(w,"clickSchedule",s);
                    ClickResult r=new ClickResult{ScheduleId=s.Id,Target=env.Origin.AddSeconds(2),Outcome="queued",DispatchAttempted=true,QueuedEvents=3,TimingError=timing?"fixture":null,CleanupError=timing?null:"fixture"};Call(w,"ConsumeResult",r);
                    Check(((Label)Field(w,"status")).ForeColor==Color.Firebrick&&(bool)Field(w,"failed"),"A faulted queued result was green.");
                }
            });
            Case("FormClosing invalidates health before FormClosed cleanup",delegate{
                using(ClockWindow w=new ClockWindow(Path.Combine(directory,"closing"),false)){
                    UiHealthState health=(UiHealthState)Field(w,"uiHealth");long generation=health.Generation;
                    typeof(Form).GetMethod("OnFormClosing",Instance).Invoke(w,new object[]{new FormClosingEventArgs(CloseReason.UserClosing,false)});
                    Check(!health.IsHealthy(generation),"Health remained valid during closing.");
                }
            });
            Case("Production no longer exposes an unguarded raw mouse-send helper",delegate{
                Check(typeof(DesktopInput).GetMethod("NativeSend",BindingFlags.Static|BindingFlags.NonPublic)==null,"Unguarded raw-send helper remains in production.");
            });
            Case("Startup failure diagnostics survive a concurrent cancellation state",delegate{
                RereviewEnvironment env=new RereviewEnvironment();using(ClickSchedule s=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100),null,env,new RereviewLease(),false)){
                    Type st=typeof(ClickSchedule).GetNestedType("ScheduleState",BindingFlags.NonPublic);ConstructorInfo ctor=st.GetConstructors(Instance)[0];ParameterInfo[] args=ctor.GetParameters();object[] values=new object[args.Length];values[0]=2;values[1]="closed";
                    Field(s,"state",ctor.Invoke(values));Call(s,"Finish",new ClickResult{ScheduleId=s.Id,Outcome="worker_start_failed"});
                    Check(s.CompletedResult.Outcome=="worker_start_failed","Cancellation erased the startup failure.");
                }
            });
            int failedCount=0;foreach(object r in rows)if(!(bool)r.GetType().GetProperty("passed").GetValue(r,null))failedCount++;
            return TimingSafety.Json(new{scope="fourth review reproductions; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=rows.Count,failed=failedCount,passed=failedCount==0,cases=rows});
        }
    }
}
