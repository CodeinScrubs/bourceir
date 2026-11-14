// Component workflow + real input only to this fixture's own harmless button.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using BrsManualClock;
using Timer=System.Windows.Forms.Timer;

namespace TimingUiTest {
    public sealed class UiFixture : Form {
        [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr window,uint message,IntPtr w,IntPtr l);
        [DllImport("user32.dll",EntryPoint="GetWindowLongW")]private static extern int GetStyle(IntPtr window,int index);
        [DllImport("user32.dll",EntryPoint="SetWindowLongW")]private static extern int SetStyle(IntPtr window,int index,int value);
        [DllImport("user32.dll")]private static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int width,int height,uint flags);
        private readonly string directory;
        private readonly bool noInput;
        private readonly ClockWindow widget;
        private readonly Button targetButton;
        private readonly Timer tick=new Timer();
        private readonly List<object> results=new List<object>();
        private readonly Stopwatch elapsed=Stopwatch.StartNew();
        private int stage,clicks;
        private double enteredAt;
        private readonly string[] lifecycleReasons={"session_changed","power_changed","desktop_changed","clock_change"};
        private int lifecycleIndex;
        public bool Passed=true;
        public UiFixture(string output,bool withoutInput) {
            directory=output;noInput=withoutInput;Directory.CreateDirectory(output);
            Text="LOCAL UI TEST ONLY - no broker";ClientSize=new Size(360,240);TopMost=true;StartPosition=FormStartPosition.Manual;
            Rectangle area=Screen.PrimaryScreen.WorkingArea;Location=new Point(area.Right-Width-15,area.Top+30);
            targetButton=new Button {Text="HARMLESS LOCAL TARGET"};targetButton.SetBounds(30,75,300,100);targetButton.Click+=delegate{clicks++;};Controls.Add(targetButton);
            widget=new ClockWindow(Path.Combine(output,"sandbox"),false);widget.StartPosition=FormStartPosition.Manual;widget.Location=new Point(area.Left,area.Top);
            tick.Interval=50;tick.Tick+=Step;
            Shown+=delegate {widget.Show();enteredAt=elapsed.Elapsed.TotalMilliseconds;tick.Start();};
            FormClosed+=delegate {
                tick.Stop();widget.Close();int failed=0;foreach(object row in results)if(!(bool)row.GetType().GetProperty("passed").GetValue(row,null))failed++;
                File.WriteAllText(Path.Combine(directory,"report.json"),TimingSafety.Json(new {scope="only local test forms",version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),no_input_mode=noInput,total=results.Count,failed=failed,passed=Passed&&failed==0,cases=results,clicks_received=clicks}),new UTF8Encoding(false));
            };
        }
        private T Field<T>(string name) {return (T)typeof(ClockWindow).GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(widget);}
        private void Call(string name,params object[] args) {typeof(ClockWindow).GetMethod(name,BindingFlags.Instance|BindingFlags.NonPublic).Invoke(widget,args);}
        private void Check(bool value,string name) {results.Add(new {name=name,passed=value});if(!value)throw new Exception(name);}
        private void EnterTarget() {
            Point p=targetButton.PointToScreen(new Point(targetButton.Width/2,targetButton.Height/2));
            widget.WindowState=FormWindowState.Minimized;
            Field<TextBox>("pointX").Text=p.X.ToString(CultureInfo.InvariantCulture);Field<TextBox>("pointY").Text=p.Y.ToString(CultureInfo.InvariantCulture);
            Check(Field<TargetSnapshot>("selectedTarget")!=null,"own fixture target selected");
        }
        private void Future() {DateTimeOffset future=ClockLogic.Now().AddSeconds(3);Field<TextBox>("clickDate").Text=ClockLogic.Jalali(future);Field<TextBox>("clickTime").Text=future.ToString("HH:mm:ss.fff",CultureInfo.InvariantCulture);}
        private void Arm() {if(Field<TargetSnapshot>("selectedTarget")==null)EnterTarget();Future();Field<Button>("clickButton").PerformClick();Check(Field<ClickSchedule>("clickSchedule")!=null,"UI created a pending schedule");enteredAt=elapsed.Elapsed.TotalMilliseconds;}
        private string LastOutcome() {
            string[] files=Directory.GetFiles(Path.Combine(directory,"sandbox","data","private","manual-clock"),"*.jsonl");
            string found=null;
            foreach(string file in files) using(FileStream stream=new FileStream(file,FileMode.Open,FileAccess.Read,FileShare.ReadWrite))using(StreamReader reader=new StreamReader(stream,Encoding.UTF8)){
                string line;while((line=reader.ReadLine())!=null){Dictionary<string,object> row=new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<Dictionary<string,object>>(line);if(Convert.ToString(row["event"])=="click_result"){Dictionary<string,object> data=(Dictionary<string,object>)row["data"];found=Convert.ToString(data["outcome"]);}}
            }
            return found;
        }
        private void Step(object sender,EventArgs args) {
            try {
                if(elapsed.Elapsed.TotalSeconds>35)throw new TimeoutException("UI fixture watchdog");
                if(stage==0) {
                    if(elapsed.Elapsed.TotalMilliseconds-enteredAt<500)return;
                    using(Bitmap image=new Bitmap(widget.Width,widget.Height)){widget.DrawToBitmap(image,new Rectangle(0,0,image.Width,image.Height));image.Save(Path.Combine(directory,"component-render.png"),ImageFormat.Png);}
                    widget.WindowState=FormWindowState.Minimized;
                    Point geometryPoint=PointToScreen(new Point(15,15));int oldStyle=GetStyle(Handle,-20);bool clientChangeRejected=false;
                    try{typeof(TargetGuard).GetMethod("CaptureForTest",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{new DesktopInput.Point{X=geometryPoint.X,Y=geometryPoint.Y},IntPtr.Zero,(Action)delegate{SetStyle(Handle,-20,oldStyle^0x200);SetWindowPos(Handle,IntPtr.Zero,0,0,0,0,0x37);}});}
                    catch(TargetInvocationException error){TargetGuardException guard=error.InnerException as TargetGuardException;clientChangeRejected=guard!=null&&guard.Reason=="target_moved";}
                    finally{SetStyle(Handle,-20,oldStyle);SetWindowPos(Handle,IntPtr.Zero,0,0,0,0,0x37);}
                    Check(clientChangeRejected,"native client-area-only change during capture is rejected");
                    int desktopReads=0;bool desktopRejected=false;
                    try{typeof(TargetGuard).GetMethod("CaptureDesktopForTest",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{new DesktopInput.Point{X=geometryPoint.X,Y=geometryPoint.Y},(Func<string>)delegate{return ++desktopReads==1?"fixture-desktop-a":"fixture-desktop-b";}});}
                    catch(TargetInvocationException error){TargetGuardException guard=error.InnerException as TargetGuardException;desktopRejected=guard!=null&&guard.Reason=="desktop_inactive";}
                    Check(desktopRejected&&desktopReads==2,"desktop change during capture is rejected by the final re-read (injected names)");
                    Point nearbyPoint=targetButton.PointToScreen(new Point(targetButton.Width/2,targetButton.Height/2));TargetSnapshot nearby=TargetGuard.Capture(new DesktopInput.Point{X=nearbyPoint.X,Y=nearbyPoint.Y},IntPtr.Zero);
                    MethodInfo nearCheck=typeof(TargetGuard).GetMethod("ValidateNearInput",BindingFlags.Static|BindingFlags.NonPublic);
                    Check(nearCheck.Invoke(null,new object[]{nearby})==null,"near-input check accepts the unchanged owned target");int oldLeft=Left;
                    foreach(string fieldName in new[]{"ProcessCreated","RootProcessCreated","RootClass","LeafClass","DisplayLayout","DesktopBounds","Monitor","QuietMilliseconds","MouseButtonsSwapped"}){
                        TargetSnapshot altered=TargetGuard.Capture(new DesktopInput.Point{X=nearbyPoint.X,Y=nearbyPoint.Y},IntPtr.Zero);FieldInfo field=typeof(TargetSnapshot).GetField(fieldName);object value=field.GetValue(altered);
                        field.SetValue(altered,value is long?(object)((long)value+1):value is string?(object)((string)value+"-fixture"):value is Rectangle?(object)new Rectangle(1,1,5,5):value is IntPtr?(object)new IntPtr(-1):value is int?(object)((int)value+1):(object)!(bool)value);
                        Check(nearCheck.Invoke(null,new object[]{altered})!=null,"near-input check rejects mismatched "+fieldName+" (owned snapshot mutation; OS settings unchanged)");
                    }
                    try{Left+=8;Check(Convert.ToString(nearCheck.Invoke(null,new object[]{nearby}))=="target_moved","near-input check catches a real move after full capture");}finally{Left=oldLeft;}
                    Field<Button>("clickButton").PerformClick();Check(Field<ClickSchedule>("clickSchedule")==null,"empty coordinates cannot arm");
                    EnterTarget();Field<TextBox>("clickDate").Text="1405/13/01";Field<Button>("clickButton").PerformClick();Check(Field<ClickSchedule>("clickSchedule")==null,"invalid date cannot arm");
                    DateTimeOffset past=ClockLogic.Now().AddSeconds(-10);Field<TextBox>("clickDate").Text=ClockLogic.Jalali(past);Field<TextBox>("clickTime").Text=past.ToString("HH:mm:ss.fff",CultureInfo.InvariantCulture);Field<Button>("clickButton").PerformClick();Check(Field<ClickSchedule>("clickSchedule")==null,"past date does not silently become tomorrow");
                    bool occupied=TargetGuard.RegisterHotKey(Handle,71,0x4000,27);Check(occupied,"Escape released after rejected schedule");
                    try {Future();Field<Button>("clickButton").PerformClick();Check(Field<ClickSchedule>("clickSchedule")==null,"hotkey conflict prevents arming");}finally{TargetGuard.UnregisterHotKey(Handle,71);}
                    if(noInput){Field<Button>("pickButton").PerformClick();Check(Field<TargetSnapshot>("selectedTarget")==null,"new selection clears the old destination");Thread.Sleep(350);stage=11;return;}
                    Point selected=targetButton.PointToScreen(new Point(targetButton.Width/2,targetButton.Height/2));
                    Rectangle work=Screen.PrimaryScreen.WorkingArea;widget.WindowState=FormWindowState.Normal;widget.Location=new Point(Math.Max(work.Left,Math.Min(selected.X-widget.Width/2,work.Right-widget.Width)),Math.Max(work.Top,Math.Min(selected.Y-widget.Height/2,work.Bottom-widget.Height)));widget.BringToFront();
                    Check(widget.Bounds.Contains(selected),"picker test begins with visible topmost clock covering target");Cursor.Position=selected;
                    Field<Button>("pickButton").PerformClick();Check(Field<TargetSnapshot>("selectedTarget")==null && Field<TextBox>("pointX").Text=="","new selection clears the old destination");stage=10;return;
                }
                if(stage==10) {
                    if(Field<bool>("picking"))return;
                    Check(Field<TargetSnapshot>("selectedTarget")!=null && Field<TargetSnapshot>("selectedTarget").Root==Handle,"timed point picker selects own fixture: "+Field<Label>("status").Text+" / "+Field<Label>("pointPreview").Text);
                    TargetSnapshot picked=Field<TargetSnapshot>("selectedTarget");Check(widget.WindowState==FormWindowState.Normal&&!widget.Bounds.Contains(picked.Point.X,picked.Point.Y)&&TargetGuard.Validate(picked)==null,"restored picker does not cover its target and fresh validation passes");
                    Field<Button>("pickButton").PerformClick();Thread.Sleep(350);stage=11;return;
                }
                if(stage==11) {
                    if(Field<bool>("picking"))return;
                    Check(Field<TargetSnapshot>("selectedTarget")==null,"stalled picker cannot retain an old destination");
                    EnterTarget();if(noInput)Field<CheckBox>("rehearsal").Checked=true;Arm();Check(!Field<TextBox>("clickDate").Enabled && !Field<TextBox>("pointX").Enabled && !Field<CheckBox>("rehearsal").Enabled,"editing and mode locked while armed");stage=1;return;
                }
                if(stage==1) {
                    if(Field<ClickSchedule>("clickSchedule")!=null)return;
                    Check(noInput?clicks==0 && LastOutcome()=="rehearsal_due":clicks==1 && LastOutcome()=="queued",(noInput?"UI rehearsal ran the scheduler and received zero clicks":"UI workflow delivered exactly one real click to own button")+"; actual outcome="+LastOutcome());
                    // Every plan remains a rehearsal in --no-input mode, including
                    // negative scenarios. A regressed guard cannot inject input.
                    Arm();PostMessage(widget.Handle,0x0312,new IntPtr(0x5ad1),new IntPtr(27<<16));stage=2;return;
                }
                if(stage==2) {
                    if(Field<ClickSchedule>("clickSchedule")!=null)return;
                    Check(clicks==(noInput?0:1) && LastOutcome()=="escape","queued Escape message cancels without requiring a held key");Arm();Call("LifecycleChanged",lifecycleReasons[lifecycleIndex]);stage=3;return;
                }
                if(stage==3) {
                    if(Field<ClickSchedule>("clickSchedule")!=null)return;
                    Check(clicks==(noInput?0:1) && LastOutcome()==lifecycleReasons[lifecycleIndex],"lifecycle signal cancels: "+lifecycleReasons[lifecycleIndex]);
                    Check(Field<TargetSnapshot>("selectedTarget")==null,"lifecycle change invalidates previously selected point");
                    lifecycleIndex++;if(lifecycleIndex<lifecycleReasons.Length){Arm();Call("LifecycleChanged",lifecycleReasons[lifecycleIndex]);return;}
                    Arm();Thread.Sleep(350);stage=4;return;
                }
                if(stage==4) {
                    if(Field<ClickSchedule>("clickSchedule")!=null)return;
                    Check(clicks==(noInput?0:1) && LastOutcome()=="ui_unresponsive","UI freeze cancels independent worker");
                    EnterTarget();Field<TimingJournal>("journal").Dispose();Future();Field<Button>("clickButton").PerformClick();
                    Check(Field<ClickSchedule>("clickSchedule")==null && clicks==(noInput?0:1),"failed durable journal prevents dispatch");
                    Check(!Field<Button>("clickButton").Enabled,"failed audit journal latches scheduling off");
                    Close();
                }
            }catch(Exception ex){Passed=false;results.Add(new {name="fixture exception",passed=false,error=ex.ToString()});Close();}
        }
    }
    public static class UiMain {
        [STAThread]public static void Main(string[] args){if(args.Length<1 || args.Length>2 || (args.Length==2 && args[1]!="--no-input"))throw new ArgumentException("Test output directory and optional --no-input required.");DesktopInput.SetDpiMode();Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);UiFixture form=new UiFixture(args[0],args.Length==2);Application.Run(form);Environment.ExitCode=form.Passed?0:1;}
    }
}
