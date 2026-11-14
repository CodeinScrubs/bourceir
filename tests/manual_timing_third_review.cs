// Third-round regression contracts. No SendInput and no broker interaction.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace BrsManualClock.Tests {
    internal sealed class FlushFaultStream : FileStream {
        internal int FlushCalls;
        internal FlushFaultStream(string path):base(path,FileMode.Open,FileAccess.Write,FileShare.Read){Position=Length;}
        public override void Flush(bool disk){base.Flush(disk);if(++FlushCalls==1)throw new IOException("owned post-write flush fault");}
    }
    internal sealed class PublishDuringFlushStream : FileStream {
        private readonly Action publish;private bool done;
        internal PublishDuringFlushStream(string path,Action publish):base(path,FileMode.Open,FileAccess.Write,FileShare.Read){Position=Length;this.publish=publish;}
        public override void Flush(bool disk){base.Flush(disk);if(!done){done=true;publish();}}
    }
    public static class ThirdReviewChecks {
        private static readonly List<object> rows=new List<object>();
        private static void Check(bool value,string error){if(!value)throw new Exception(error);}
        private static void Case(string name,Action test){try{test();rows.Add(new{name=name,passed=true,error=(string)null});}catch(Exception ex){rows.Add(new{name=name,passed=false,error=ex.GetBaseException().Message});}}
        private static TargetSnapshot Snapshot(){return new TargetSnapshot{Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=550};}
        private static ClickResult Done(ClickSchedule schedule){Stopwatch wait=Stopwatch.StartNew();while(schedule.CompletedResult==null&&wait.ElapsedMilliseconds<4000)Thread.Sleep(1);Check(schedule.CompletedResult!=null,"No result.");return schedule.CompletedResult;}
        private static FlushFaultStream ReplaceStream(TimingJournal journal){FieldInfo field=typeof(TimingJournal).GetField("stream",BindingFlags.Instance|BindingFlags.NonPublic);((FileStream)field.GetValue(journal)).Dispose();FlushFaultStream replacement=new FlushFaultStream(journal.PathName);field.SetValue(journal,replacement);return replacement;}
        public static string RunAll(string directory){
            rows.Clear();Directory.CreateDirectory(directory);
            Case("Small rollback at dispatcher entry resumes a known no-input attempt",delegate{
                RereviewEnvironment env=new RereviewEnvironment();bool shifted=false;env.BeforeDispatch=delegate{if(!shifted){shifted=true;env.Shift-=0.5;}};
                ClickPlan plan=new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100);
                using(ClickSchedule schedule=new ClickSchedule(plan,null,env,new RereviewLease(),true)){ClickResult result=Done(schedule);Check(result.Outcome=="queued"&&env.Mouse.Calls==1&&result.SendStarted>=plan.Target,"Pre-commit early sample consumed the schedule.");}
            });
            Case("Large rollback at dispatcher entry is classified as clock change",delegate{
                RereviewEnvironment env=new RereviewEnvironment();bool shifted=false;env.BeforeDispatch=delegate{if(!shifted){shifted=true;env.Shift-=150;}};
                using(ClickSchedule schedule=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100),null,env,new RereviewLease(),true)){ClickResult result=Done(schedule);Check(result.Outcome=="clock_change"&&env.Mouse.Calls==0,"Large rollback was mislabeled or sent input.");}
            });
            Case("All entered dispatcher exit paths retain a method-finished sample",delegate{
                foreach(int mode in new[]{0,1,2,3,4}){
                    RereviewEnvironment env=new RereviewEnvironment();ClickPlan plan=new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100,null,mode==3);ClickResult result=new ClickResult();int reads=0;
                    InputDispatcher.Dispatch(plan,result,delegate{return plan.Target.AddMilliseconds(mode==0?-1:mode==1?101:mode==4&&++reads>1?101:0);},env.Mouse,delegate{return mode!=2;});
                    Check(result.DispatchFinishedAt.HasValue,"Dispatcher mode "+mode+" has no completion sample.");Check(result.NativeCallEntrySample==null,"Synthetic exit invented native timing.");
                }
            });
            Case("A post-write flush failure poisons the journal instead of appending again",delegate{
                using(TimingJournal journal=new TimingJournal(Path.Combine(directory,"journal-fault"))){
                    FlushFaultStream stream=ReplaceStream(journal);bool first=false,second=false;
                    try{journal.Append("fixture_record",new{known=true});}catch(IOException){first=true;}
                    try{journal.Append("fixture_record",new{known=true});}catch(Exception){second=true;}
                    journal.Dispose();Check(first&&second&&stream.FlushCalls==1&&File.ReadAllLines(journal.PathName).Length==1,"A failed durability attempt was retried into the same journal.");
                }
            });
            Case("Result persistence does not duplicate a write whose flush failed",delegate{
                using(ClockWindow window=new ClockWindow(Path.Combine(directory,"result-fault"),false)){
                    TimingJournal journal=(TimingJournal)typeof(ClockWindow).GetField("journal",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(window);ReplaceStream(journal);
                    MethodInfo record=typeof(ClockWindow).GetMethod("RecordResultOnce",BindingFlags.Instance|BindingFlags.NonPublic);ClickResult result=new ClickResult{ScheduleId="owned-fixture",Target=DateTimeOffset.UtcNow,Outcome="queued"};
                    bool first=(bool)record.Invoke(window,new object[]{result,null});bool second=(bool)record.Invoke(window,new object[]{result,null});
                    journal.Dispose();int count=0;foreach(string line in File.ReadAllLines(journal.PathName))if(line.Contains("\"event\":\"click_result\""))count++;
                    Check(!first&&!second&&count==1,"An uncertain result append was duplicated or called durable.");
                }
            });
            Case("Distant click schedules are rejected before starting an awake worker",delegate{
                RereviewEnvironment env=new RereviewEnvironment();bool rejected=false;
                try{using(ClickSchedule schedule=new ClickSchedule(new ClickPlan(env.Origin.AddDays(2),Snapshot(),100),null,env,new RereviewLease(),false)){};}catch(Exception error){rejected=error.GetType().Name=="ScheduleHorizonException";}
                Check(rejected&&env.Mouse.Calls==0,"Unbounded future target accepted.");
            });
            Case("Close distinguishes persisted worker record from destination processing",delegate{
                string workspace=Path.Combine(directory,"close-semantics");using(ClockWindow window=new ClockWindow(workspace,false)){
                    typeof(ClockWindow).GetMethod("HandleClosed",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(window,new object[]{window,new FormClosedEventArgs(CloseReason.UserClosing)});
                    string log=File.ReadAllText(Directory.GetFiles(Path.Combine(workspace,"data","private","manual-clock"),"*.jsonl")[0]);
                    Check(log.Contains("\"worker_result_record_unresolved\":false")&&log.Contains("\"destination_outcome_verified\":false"),"Close labels do not distinguish the worker journal from destination processing.");
                }
            });
            Case("Close reconciles a worker result published while shutdown_pending is flushed",delegate{
                string workspace=Path.Combine(directory,"close-publication");RereviewEnvironment env=new RereviewEnvironment();
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false))using(ClockWindow window=new ClockWindow(workspace,false)){
                    env.Mouse.OnCall=delegate(int count){entered.Set();release.WaitOne(4000);};ClickSchedule schedule=new ClickSchedule(new ClickPlan(env.Origin.AddSeconds(2),Snapshot(),100),null,env,new RereviewLease(),true);
                    try{
                        Check(entered.WaitOne(2000),"Owned dispatch barrier absent.");typeof(ClockWindow).GetField("clickSchedule",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(window,schedule);
                        TimingJournal journal=(TimingJournal)typeof(ClockWindow).GetField("journal",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(window);FieldInfo field=typeof(TimingJournal).GetField("stream",BindingFlags.Instance|BindingFlags.NonPublic);((FileStream)field.GetValue(journal)).Dispose();
                        field.SetValue(journal,new PublishDuringFlushStream(journal.PathName,delegate{release.Set();Done(schedule);}));
                        typeof(ClockWindow).GetMethod("HandleClosed",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(window,new object[]{window,new FormClosedEventArgs(CloseReason.UserClosing)});
                        string log=File.ReadAllText(journal.PathName);Check(log.Contains("\"event\":\"shutdown_pending\"")&&log.Contains("\"event\":\"click_result\""),"Result published during journal flush was lost.");
                    }finally{release.Set();Done(schedule);schedule.Dispose();}
                }
            });
            int failed=0;foreach(object row in rows)if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;
            return TimingSafety.Json(new{scope="third-round contracts; synthetic input and owned file faults only",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=rows.Count,failed=failed,passed=failed==0,cases=rows});
        }
    }
}
