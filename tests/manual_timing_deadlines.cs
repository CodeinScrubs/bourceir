// Deadline regressions only. Uses the test environment; never sends desktop input.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Threading;
using System.Diagnostics;

namespace BrsManualClock.Tests {
    public static class DeadlineChecks {
        private static TargetSnapshot Snapshot() {
            return new TargetSnapshot { Point=new DesktopInput.Point {X=10,Y=10},Root=new IntPtr(11),Leaf=new IntPtr(12),DesktopBounds=new Rectangle(0,0,100,100),QuietMilliseconds=1 };
        }
        private static ClickResult Run(TestEnvironment environment,double tolerance) {
            ClickPlan plan=new ClickPlan(environment.Origin.AddSeconds(2),Snapshot(),tolerance);
            using(ClickSchedule schedule=new ClickSchedule(plan,null,environment,new TestLease(),true)) {
                Stopwatch limit=Stopwatch.StartNew();
                while(schedule.CompletedResult==null && limit.ElapsedMilliseconds<3000) Thread.Sleep(1);
                if(schedule.CompletedResult==null) throw new Exception("Test schedule did not finish.");
                return schedule.CompletedResult;
            }
        }
        private static void Require(bool value,string reason) { if(!value)throw new Exception(reason); }
        private static void Case(List<object> cases,string name,Action body) {
            try {body();cases.Add(new {name=name,passed=true,error=(string)null});}
            catch(Exception ex){cases.Add(new {name=name,passed=false,error=ex.Message});}
        }
        public static string RunAll() {
            List<object> cases=new List<object>();
            Case(cases,"1 ms dispatch boundary is accepted",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),1);ClickResult r=new ClickResult();
                InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddTicks(10000);},e.Mouse,delegate{return true;});
                Require(r.Outcome=="queued" && e.Mouse.Calls.Count==1 && r.LatenessMs==1,"Expected one simulated dispatch at the exact 1 ms boundary.");
            });
            Case(cases,"One timestamp tick beyond 1 ms sends no input",delegate {
                TestEnvironment e=new TestEnvironment();ClickPlan p=new ClickPlan(e.Origin.AddSeconds(2),Snapshot(),1);ClickResult r=new ClickResult();
                InputDispatcher.Dispatch(p,r,delegate{return p.Target.AddTicks(10001);},e.Mouse,delegate{return true;});
                Require(r.Outcome=="too_late" && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Exceeded 1 ms deadline was dispatched.");
            });
            Case(cases,"Slow final target validation cannot bypass the deadline",delegate {
                TestEnvironment e=new TestEnvironment();e.AtGuard=delegate{e.Mono+=5;};ClickResult r=Run(e,1);
                Require(r.Outcome=="too_late" && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Slow target check bypassed 1 ms deadline.");
            });
            Case(cases,"Clock moving backwards inside target validation sends no input",delegate {
                TestEnvironment e=new TestEnvironment();e.AtGuard=delegate{e.Shift-=200;};ClickResult r=Run(e,100);
                Require(!r.DispatchAttempted && e.Mouse.Calls.Count==0,"Clock moved backwards but dispatch was attempted.");
            });
            Case(cases,"Guard stall masked by a backwards clock correction cancels",delegate {
                TestEnvironment e=new TestEnvironment();e.AtGuard=delegate{e.Mono+=500;e.Shift-=450;};ClickResult r=Run(e,100);
                Require(!r.DispatchAttempted && e.Mouse.Calls.Count==0,"A 500 ms guard stall and -450 ms clock correction bypassed continuity checks: "+r.Outcome);
            });
            Case(cases,"Cumulative clock correction during guard cancels",delegate {
                TestEnvironment e=new TestEnvironment();bool adjusted=false;
                e.Paused=delegate(TestEnvironment clock){if(!adjusted){adjusted=true;clock.Shift+=60;}};
                e.AtGuard=delegate{e.Shift+=60;};ClickResult r=Run(e,100);
                Require(r.Outcome=="clock_change" && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Final cumulative clock drift bypassed continuity checks.");
            });
            Case(cases,"Long guard stall cancels even inside a wide dispatch window",delegate {
                TestEnvironment e=new TestEnvironment();e.AtGuard=delegate{e.Mono+=300;};ClickResult r=Run(e,5000);
                Require(r.Outcome=="scheduler_gap" && !r.DispatchAttempted && e.Mouse.Calls.Count==0,"Long final guard stall bypassed scheduling-gap detection.");
            });
            Case(cases,"Short unchanged-clock guard remains eligible inside its window",delegate {
                TestEnvironment e=new TestEnvironment();e.AtGuard=delegate{e.Mono+=50;};ClickResult r=Run(e,100);
                Require(r.Outcome=="queued" && e.Mouse.Calls.Count==1,"Valid finite guard delay was incorrectly rejected.");
            });
            int failed=0;foreach(object entry in cases)if(!(bool)entry.GetType().GetProperty("passed").GetValue(entry,null))failed++;
            return TimingSafety.Json(new {scope="deterministic deadline regression; simulated clock and sink; no desktop input",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),passed=failed==0,total=cases.Count,failed=failed,cases=cases});
        }
    }
}
