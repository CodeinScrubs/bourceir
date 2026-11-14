// Second-round review reproductions. Synthetic queues only; no SendInput calls.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace BrsManualClock.Tests {
    internal sealed class RereviewLease : IDisposable { public void Dispose(){} }
    internal sealed class RereviewMouse : IMouseQueue {
        internal int Calls;
        internal uint Count=3;
        internal Action<int> OnCall;
        public RawSend Send(MousePacket[] packets) { Calls++;if(OnCall!=null)OnCall(Calls);return new RawSend(packets.Length==1?1:Count,0); }
    }
    internal sealed class RereviewEnvironment : IClickEnvironment {
        internal readonly DateTimeOffset Origin=DateTimeOffset.Parse("2030-01-02T00:00:00Z");
        internal readonly RereviewMouse Mouse=new RereviewMouse();
        internal double Mono,Shift;
        internal Action AtGuard,BeforeDispatch;
        public DateTimeOffset Now(){return Origin.AddTicks((long)((Mono+Shift)*10000));}
        public double Monotonic{get{return Mono;}}
        public uint CurrentTick{get{return unchecked((uint)(1000000+Mono));}}
        public uint LastInput{get{return 0;}}
        public bool Escape{get{return false;}}public bool ButtonsHeld{get{return false;}}
        public bool HighResolution{get{return true;}} // Synthetic capability, not a hardware measurement.
        public string ValidateTarget(ClickPlan p){if(AtGuard!=null)AtGuard();return null;}
        public void Pause(double ms,WaitHandle signal){Mono+=ms<=0?0.25:ms;}
        public void Dispatch(ClickPlan p,ClickResult r,Func<bool> gate){if(BeforeDispatch!=null)BeforeDispatch();InputDispatcher.Dispatch(p,r,Now,Mouse,gate);}
        public void Dispose(){}
    }
    public static class RereviewChecks {
        [DllImport("gdi32.dll")]private static extern bool DeleteObject(IntPtr handle);
        private static readonly List<object> cases=new List<object>();
        private static void Require(bool ok,string message){if(!ok)throw new Exception(message);}
        private static void Case(string name,Action body){try{body();cases.Add(new{name=name,passed=true,error=(string)null});}catch(Exception ex){cases.Add(new{name=name,passed=false,error=ex.GetBaseException().Message});}}
        private static TargetSnapshot Snapshot(){return new TargetSnapshot{Point=new DesktopInput.Point{X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=550};}
        private static ClickPlan Plan(RereviewEnvironment e,Func<bool> health=null){return new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),100,health);}
        private static ClickResult Done(ClickSchedule s){Stopwatch watch=Stopwatch.StartNew();while(s.CompletedResult==null&&watch.ElapsedMilliseconds<4000)Thread.Sleep(1);Require(s.CompletedResult!=null,"Worker did not publish a result.");return s.CompletedResult;}
        private static void CloseForTest(ClockWindow w){typeof(ClockWindow).GetMethod("HandleClosed",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(w,new object[]{w,new FormClosedEventArgs(CloseReason.UserClosing)});}
        public static string RunAll(string directory){
            cases.Clear();Directory.CreateDirectory(directory);
            Case("Null commit gate cannot send input",delegate{
                RereviewEnvironment e=new RereviewEnvironment();ClickPlan p=Plan(e);ClickResult r=new ClickResult();bool rejected=false;
                try{InputDispatcher.Dispatch(p,r,delegate{return p.Target;},e.Mouse,null);}catch(ArgumentNullException){rejected=true;}
                Require(rejected&&e.Mouse.Calls==0&&!r.DispatchAttempted,"A missing commit gate was accepted.");
            });
            Case("Clock rollback after final target check cannot hide a scheduler stall",delegate{
                RereviewEnvironment e=new RereviewEnvironment();e.BeforeDispatch=delegate{e.Mono+=400;e.Shift-=350;};
                using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true)){ClickResult r=Done(s);Require(r.Outcome=="clock_change"&&e.Mouse.Calls==0,"Post-validation rollback/stall reached the queue.");}
            });
            Case("UI health expiring during target validation sends zero input",delegate{
                RereviewEnvironment e=new RereviewEnvironment();bool healthy=true;ClickPlan p=Plan(e,delegate{return healthy;});
                // The same source runs on 2.1.1 and the fixed build. The old build
                // lacks the trusted monitor; its arbitrary callback was pre-query only.
                Type monitorType=typeof(ClickPlan).Assembly.GetType("BrsManualClock.UiHealthState");
                object monitor=monitorType==null?null:Activator.CreateInstance(monitorType);
                if(monitor!=null)typeof(ClickPlan).GetField("HealthState",BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic).SetValue(p,monitor);
                e.AtGuard=delegate{healthy=false;if(monitor!=null)monitorType.GetMethod("Fail").Invoke(monitor,null);};
                using(ClickSchedule s=new ClickSchedule(p,null,e,new RereviewLease(),true)){ClickResult r=Done(s);Require(r.Outcome=="ui_unresponsive"&&e.Mouse.Calls==0,"Stale post-query health reached dispatch.");}
            });
            Case("Send return sample precedes release-only cleanup",delegate{
                RereviewEnvironment e=new RereviewEnvironment();ClickPlan p=Plan(e);e.Mono=2000;e.Mouse.Count=2;e.Mouse.OnCall=delegate(int n){e.Mono+=n==1?1:200;};ClickResult r=new ClickResult();
                InputDispatcher.Dispatch(p,r,e.Now,e.Mouse,delegate{return true;});
                Require(r.SendReturned==e.Origin.AddMilliseconds(2001)&&e.Mouse.Calls==2,"Click return time includes later release cleanup.");
            });
            Case("Successful later Dispose clears current timeout state",delegate{
                RereviewEnvironment e=new RereviewEnvironment();
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false)){
                    e.AtGuard=delegate{entered.Set();Require(release.WaitOne(4000),"Fixture barrier timeout.");};
                    ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true);
                    try{Require(entered.WaitOne(2000),"Guard not entered.");s.Dispose();Require(s.DisposeTimedOut,"First join did not expose the timeout.");}
                    finally{release.Set();}
                    Done(s);s.Dispose();Require(!s.DisposeTimedOut&&s.ShutdownError==null,"A completed later join still reports a current timeout.");
                }
            });
            Case("Prepared Dispose does not wait for a blocked notification",delegate{
                RereviewEnvironment e=new RereviewEnvironment();
                using(ManualResetEvent entered=new ManualResetEvent(false))using(ManualResetEvent release=new ManualResetEvent(false))using(ManualResetEvent disposed=new ManualResetEvent(false)){
                    ClickSchedule s=new ClickSchedule(Plan(e),delegate{entered.Set();release.WaitOne(3000);},e,new RereviewLease(),false);
                    Thread t=new Thread(delegate(){try{s.Dispose();}finally{disposed.Set();}});t.IsBackground=true;t.Start();
                    bool returned=false;try{Require(entered.WaitOne(2000),"Notification did not run.");returned=disposed.WaitOne(700);}finally{release.Set();Require(t.Join(2000),"Dispose fixture did not exit.");s.Dispose();}
                    Require(returned&&e.Mouse.Calls==0,"Prepared disposal was held by the user callback.");
                }
            });
            Case("Drive-root paths remain rooted and can be checked read-only",delegate{
                string root=Path.GetPathRoot(Path.GetFullPath(directory));string expected=Path.Combine(root,"TimingClock-read-only-path-check");
                Require(TimingSafety.RequireSafePath(expected,root)==Path.GetFullPath(expected),"Drive root became a drive-relative path.");
            });
            Case("Future last-input tick cannot wrap into a quiet interval",delegate{
                InputActivity a=new InputActivity();a.Observe(1001,0);Require(!a.Quiet(1000,600,550),"Future tick was interpreted as 49 days of quiet.");
                InputActivity b=new InputActivity();b.Observe(UInt32.MaxValue-100,0);Require(b.Quiet(600,1000,550),"Normal unsigned tick rollover was rejected.");
            });
            Case("Wrong-thread GUI lease disposal preserves owner recovery",delegate{
                string name="Local\\BrsTimingClock-rereview-"+Guid.NewGuid().ToString("N");ApplicationLease first=new ApplicationLease(name,delegate{});bool rejected=false,reacquired=false;
                try{
                    Thread wrong=new Thread(delegate(){try{first.Dispose();}catch{rejected=true;}});wrong.Start();Require(wrong.Join(2000),"Foreign disposer hung.");Require(rejected,"Foreign thread released the GUI mutex.");
                    first.Dispose();Thread next=new Thread(delegate(){try{using(ApplicationLease second=new ApplicationLease(name,delegate{})){reacquired=true;}}catch{}});next.Start();Require(next.Join(2000),"Next owner hung.");
                }finally{
                    // Only this fixture's uniquely named mutex; repair old-code
                    // cleanup so a deliberately failing baseline leaves no handle.
                    Mutex mutex=(Mutex)typeof(ApplicationLease).GetField("mutex",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(first);
                    try{mutex.ReleaseMutex();}catch(ApplicationException){}catch(ObjectDisposedException){}mutex.Dispose();
                }
                Require(reacquired,"Wrong-thread Dispose poisoned ownership and owner retry.");
            });
            Case("Shutdown persists an already completed result without the UI callback",delegate{
                string workspace=Path.Combine(directory,"close-result-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(workspace);RereviewEnvironment e=new RereviewEnvironment();
                using(ClockWindow w=new ClockWindow(workspace,false))using(ClickSchedule s=new ClickSchedule(Plan(e),null,e,new RereviewLease(),true)){
                    Done(s);typeof(ClockWindow).GetField("clickSchedule",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(w,s);CloseForTest(w);
                    string[] files=Directory.GetFiles(Path.Combine(workspace,"data","private","manual-clock"),"*.jsonl");Require(files.Length==1,"Fixture journal missing.");
                    Require(File.ReadAllText(files[0]).Contains("\"event\":\"click_result\""),"Closing lost the completed result before its UI callback.");
                }
            });
            Case("Owned custom fonts are disposed with the clock form",delegate{
                string workspace=Path.Combine(directory,"fonts-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(workspace);Font font;
                using(ClockWindow w=new ClockWindow(workspace,false)){font=w.Font;CloseForTest(w);}bool closed=false;IntPtr handle=IntPtr.Zero;
                try{handle=font.ToHfont();}catch(ArgumentException){closed=true;}catch(ObjectDisposedException){closed=true;}finally{if(handle!=IntPtr.Zero)DeleteObject(handle);font.Dispose();}
                Require(closed,"Assigned custom Font remained live after form disposal.");
            });
            int failed=0;foreach(object row in cases)if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;
            return TimingSafety.Json(new{scope="Second-round review reproductions; synthetic queues, no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=cases.Count,failed=failed,passed=failed==0,cases=cases});
        }
    }
}
