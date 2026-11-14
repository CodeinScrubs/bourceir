// Two owned processes, one hosted native control. No mouse or keyboard input.
using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using BrsManualClock;
using Timer=System.Windows.Forms.Timer;
namespace HostedGuardTest {
    public sealed class Host : Form {
        private readonly string directory;
        private readonly Panel panel=new Panel();
        private readonly Timer timer=new Timer();
        private readonly Stopwatch watch=Stopwatch.StartNew();
        private Process child;
        public bool Passed;
        public Host(string output) {
            directory=output;Directory.CreateDirectory(output);Text="LOCAL HOSTED-CONTROL GUARD TEST - no input";ClientSize=new Size(430,240);TopMost=true;StartPosition=FormStartPosition.Manual;Rectangle screen=Screen.PrimaryScreen.WorkingArea;Location=new Point(screen.Right-Width-10,screen.Top+60);
            panel.SetBounds(25,70,380,110);Controls.Add(panel);
            Shown+=delegate {
                child=Process.Start(new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName,"--child "+panel.Handle.ToInt64().ToString(CultureInfo.InvariantCulture)+" \""+directory+"\""){UseShellExecute=false,CreateNoWindow=true});
                IntPtr unused=child.Handle;timer.Interval=100;timer.Start();
            };
            timer.Tick+=delegate {
                try {
                    if(watch.Elapsed.TotalSeconds>10)throw new TimeoutException("Hosted fixture startup");
                    if(File.Exists(Path.Combine(directory,"child-error.txt")))throw new Exception(File.ReadAllText(Path.Combine(directory,"child-error.txt")));
                    if(!File.Exists(Path.Combine(directory,"child-ready.txt")))return;
                    Point p=panel.PointToScreen(new Point(130,45));TargetSnapshot snapshot=TargetGuard.Capture(new DesktopInput.Point{X=p.X,Y=p.Y},IntPtr.Zero);
                    Passed=snapshot.Root==Handle && snapshot.RootProcessId==(uint)Process.GetCurrentProcess().Id && snapshot.ProcessId==(uint)child.Id && snapshot.ProcessId!=snapshot.RootProcessId && TargetGuard.Validate(snapshot)==null;
                    File.WriteAllText(Path.Combine(directory,"report.json"),TimingSafety.Json(new {version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=1,failed=Passed?0:1,passed=Passed,scope="two owned processes; capture and validation only",cross_process_leaf=snapshot.ProcessId!=snapshot.RootProcessId,input_sent=false}),new UTF8Encoding(false));Close();
                }catch(Exception ex){File.WriteAllText(Path.Combine(directory,"report.json"),TimingSafety.Json(new {version=TimingSafety.Version,recorded_at_tehran=TimingSafety.Iso(DateTimeOffset.UtcNow),total=1,failed=1,passed=false,error=ex.ToString(),input_sent=false}),new UTF8Encoding(false));Close();}
            };
            FormClosed+=delegate {timer.Stop();if(child!=null){try{if(!child.HasExited)child.Kill();}catch(InvalidOperationException){}}};
        }
    }
    public static class Program {
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]private static extern IntPtr CreateWindowEx(uint ex,string className,string text,uint style,int x,int y,int width,int height,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr parameter);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]private static extern IntPtr GetModuleHandle(string name);
        [STAThread]public static void Main(string[] args){
            DesktopInput.SetDpiMode();Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            if(args.Length==3 && args[0]=="--child"){
                try{IntPtr target=CreateWindowEx(0,"BUTTON","HOSTED LOCAL TARGET",0x50000000,5,5,300,90,new IntPtr(Int64.Parse(args[1],CultureInfo.InvariantCulture)),IntPtr.Zero,GetModuleHandle(null),IntPtr.Zero);if(target==IntPtr.Zero)throw new Exception("CreateWindowEx error "+Marshal.GetLastWin32Error());File.WriteAllText(Path.Combine(args[2],"child-ready.txt"),"ready");Application.Run();}
                catch(Exception ex){File.WriteAllText(Path.Combine(args[2],"child-error.txt"),ex.ToString());Environment.ExitCode=1;}return;
            }
            if(args.Length!=1)throw new ArgumentException("Output directory required.");Host host=new Host(args[0]);Application.Run(host);Environment.ExitCode=host.Passed?0:1;
        }
    }
}
