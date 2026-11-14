// Windows safety checks and durable local receipts. Never activates another app.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.ExceptionServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace BrsManualClock {
    public static class TimingSafety {
        public const string Version = "2.1.4";
        public static readonly TimeZoneInfo Tehran = TimeZoneInfo.FindSystemTimeZoneById("Iran Standard Time");
        public static bool Finite(double value) { return !Double.IsNaN(value) && !Double.IsInfinity(value); }
        public static string Json(object value) { return new JavaScriptSerializer().Serialize(value); }
        public static string Iso(DateTimeOffset value) { return TimeZoneInfo.ConvertTime(value, Tehran).ToString("o", CultureInfo.InvariantCulture); }
        public static string FileHash(string path) { using (SHA256 hash = SHA256.Create()) using (FileStream stream = File.OpenRead(path)) return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant(); }
        internal static string UserKernelName(string purpose) { return "Local\\BrsTimingClock-" + purpose + "-" + WindowsIdentity.GetCurrent().User.Value; }
        public static string PrivateFolder(string workspace) { return RequireSafePath(Path.Combine(Path.GetFullPath(workspace), "data", "private", "manual-clock"), workspace); }
        internal static string DirectoryRoot(string directory) {
            string full=Path.GetFullPath(directory), drive=Path.GetPathRoot(full);
            return full.Length==drive.Length ? full : full.TrimEnd(Path.DirectorySeparatorChar,Path.AltDirectorySeparatorChar);
        }
        public static bool IsInside(string path, string directory) {
            string full=Path.GetFullPath(path),root=DirectoryRoot(directory);
            string prefix=root.EndsWith(Path.DirectorySeparatorChar.ToString(),StringComparison.Ordinal)?root:root+Path.DirectorySeparatorChar;
            return !String.Equals(full,root,StringComparison.OrdinalIgnoreCase) && full.StartsWith(prefix,StringComparison.OrdinalIgnoreCase);
        }
        internal static string ErrorKinds(Exception error) {
            AggregateException many=error as AggregateException;
            if(many==null)return error.GetType().Name;
            List<string> names=new List<string>();foreach(Exception child in many.InnerExceptions)names.Add(ErrorKinds(child));
            return "AggregateException["+String.Join(",",names.ToArray())+"]";
        }
        // Guards accidental redirects through existing junctions/symlinks. This
        // is not a handle-based sandbox against a concurrent same-user attacker.
        public static string RequireSafePath(string path, string workspace) {
            string full=Path.GetFullPath(path), root=DirectoryRoot(workspace);
            if (!String.Equals(full,root,StringComparison.OrdinalIgnoreCase) && !IsInside(full,root)) throw new IOException("Generated path escapes the workspace.");
            string current=full;
            for (;;) {
                try { if ((File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0) throw new IOException("Reparse points are not allowed in generated paths."); }
                catch (FileNotFoundException) { } catch (DirectoryNotFoundException) { }
                if (String.Equals(current,root,StringComparison.OrdinalIgnoreCase)) break;
                current=Path.GetDirectoryName(current);
                if (String.IsNullOrEmpty(current)) throw new IOException("Generated path containment could not be checked.");
            }
            return full;
        }
    }

    // Owned state only: final worker reads never invoke an application callback.
    public sealed class UiHealthState {
        private sealed class Health {internal readonly long Generation;internal readonly bool Failed;internal readonly string Reason;internal Health(long generation,bool failed,string reason){Generation=generation;Failed=failed;Reason=reason;}}
        private Health state=new Health(0,false,null);
        private long heartbeat=Stopwatch.GetTimestamp();
        public long Generation { get { return Volatile.Read(ref state).Generation; } }
        public void Pulse() { Interlocked.Exchange(ref heartbeat,Stopwatch.GetTimestamp()); }
        public void Invalidate() { InvalidateWithReason("ui_unresponsive"); }
        public void InvalidateWithReason(string reason) { Change(false,reason); }
        public void Fail() { FailWithReason("ui_unresponsive"); }
        public void FailWithReason(string reason) { Change(true,reason); }
        private void Change(bool fail,string reason){for(;;){Health old=Volatile.Read(ref state);Health next=new Health(old.Generation+1,old.Failed||fail,reason??"ui_unresponsive");if(Interlocked.CompareExchange(ref state,next,old)==old)return;}}
        internal void PulseAt(long value) { Interlocked.Exchange(ref heartbeat,value); }
        public bool IsHealthy(long expectedGeneration) {
            Health before=Volatile.Read(ref state);long beat=Interlocked.Read(ref heartbeat),now=Stopwatch.GetTimestamp();
            return !before.Failed && before.Generation==expectedGeneration && now>=beat &&
                (now-beat)*1000.0/Stopwatch.Frequency<=250 && Object.ReferenceEquals(Volatile.Read(ref state),before);
        }
        internal string RejectionReason(long expectedGeneration){Health current=Volatile.Read(ref state);if(current.Failed||current.Generation!=expectedGeneration)return current.Reason??"ui_unresponsive";return IsHealthy(expectedGeneration)?null:"ui_unresponsive";}
    }

    internal static class InputPrivilege {
        [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access,bool inherit,uint process);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll")] private static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)] private static extern bool GetTokenInformation(IntPtr token,int information,IntPtr buffer,uint size,out uint needed);
        internal static string Difference(uint current,uint leaf,uint root) { return leaf>current || root>current?"target_integrity_higher":null; }
        internal static uint Read(uint process) {
            IntPtr owner=OpenProcess(0x1000,false,process),token=IntPtr.Zero,buffer=IntPtr.Zero;
            try {
                if(owner==IntPtr.Zero || !OpenProcessToken(owner,8,out token))throw new TargetGuardException("target_integrity_unknown");
                uint needed;bool sized=GetTokenInformation(token,25,IntPtr.Zero,0,out needed);
                if(sized || Marshal.GetLastWin32Error()!=122 || needed<IntPtr.Size || needed>65536)throw new TargetGuardException("target_integrity_unknown");
                buffer=Marshal.AllocHGlobal((int)needed);
                if(!GetTokenInformation(token,25,buffer,needed,out needed))throw new TargetGuardException("target_integrity_unknown");
                string sid=new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value;
                uint level;if(!sid.StartsWith("S-1-16-",StringComparison.Ordinal) || !UInt32.TryParse(sid.Substring(7),NumberStyles.None,CultureInfo.InvariantCulture,out level))throw new TargetGuardException("target_integrity_unknown");
                return level;
            } finally {if(buffer!=IntPtr.Zero)Marshal.FreeHGlobal(buffer);if(token!=IntPtr.Zero)CloseHandle(token);if(owner!=IntPtr.Zero)CloseHandle(owner);}
        }
        internal static void Require(TargetSnapshot target) {
            uint current=Read((uint)Process.GetCurrentProcess().Id),leaf=Read(target.ProcessId);
            uint root=target.RootProcessId==target.ProcessId?leaf:Read(target.RootProcessId);
            string difference=Difference(current,leaf,root);
            if(difference!=null)throw new TargetGuardException(difference);
        }
    }

    internal sealed class WindowsExecutionLease : IDisposable {
        [DllImport("kernel32.dll")] private static extern uint SetThreadExecutionState(uint flags);
        private readonly Func<uint,uint> change;
        private readonly uint previous;
        private readonly int thread;
        private int disposed;
        public WindowsExecutionLease() : this(SetThreadExecutionState) { }
        internal WindowsExecutionLease(Func<uint,uint> setter) {
            change=setter;thread=Thread.CurrentThread.ManagedThreadId;
            previous=change(0x80000003);
            if(previous==0)throw new InvalidOperationException("Windows could not reserve the awake/display state.");
        }
        public void Dispose() {
            if(Volatile.Read(ref disposed)!=0)return;
            if(Thread.CurrentThread.ManagedThreadId!=thread)throw new InvalidOperationException("Execution state must be restored on its owning thread.");
            if(change(previous|0x80000000)==0)throw new InvalidOperationException("Windows could not restore the previous execution state.");
            Volatile.Write(ref disposed,1);
        }
    }

    internal sealed class JournalPrewriteException : Exception {internal JournalPrewriteException(Exception error):base("Journal preparation failed before any write.",error){}}
    public sealed class TimingJournal : IDisposable {
        private readonly object gate = new object();
        private readonly FileStream stream;
        private readonly Func<DateTimeOffset> clock=DesktopInput.PreciseUtcNow;
        private long sequence;
        private bool closed;
        private bool faulted;
        public readonly string PathName;
        private readonly string workspaceRoot;
        public TimingJournal(string workspace) {
            workspaceRoot=workspace;
            string directory = TimingSafety.PrivateFolder(workspace);
            Directory.CreateDirectory(directory);
            TimingSafety.RequireSafePath(directory,workspace);
            PathName = Path.Combine(directory, "clock-" + DateTimeOffset.UtcNow.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture) + "-" + Process.GetCurrentProcess().Id + "-" + Guid.NewGuid().ToString("N") + ".jsonl");
            TimingSafety.RequireSafePath(PathName,workspace);
            stream = new FileStream(PathName, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 4096, FileOptions.WriteThrough);
        }
        public void Append(string kind, object data) {
            lock (gate) {
                if (closed) throw new ObjectDisposedException("TimingJournal");
                if(faulted)throw new InvalidOperationException("The journal has an unresolved write failure; appending again is unsafe.");
                long candidate=checked(sequence+1);byte[] bytes;
                try{string line=TimingSafety.Json(new{schema_version=4,sequence=candidate,time_tehran=TimingSafety.Iso(clock()),@event=kind,data=data})+"\n";bytes=new UTF8Encoding(false).GetBytes(line);}
                catch(Exception error){throw new JournalPrewriteException(error);}
                // A flush failure may follow a complete or partial write. It is
                // not atomic: latch failure and never retry into this stream.
                try{stream.Write(bytes, 0, bytes.Length);stream.Flush(true);sequence=candidate;}
                catch{faulted=true;throw;}
            }
        }
        internal bool TryFailureMarker(string scope,Exception error){
            try{string marker=PathName+".failed-"+Guid.NewGuid().ToString("N")+".json";TimingSafety.RequireSafePath(marker,workspaceRoot);
                byte[] bytes=new UTF8Encoding(false).GetBytes(TimingSafety.Json(new{schema_version=1,scope=scope,error_types=TimingSafety.ErrorKinds(error),last_confirmed_sequence=Interlocked.Read(ref sequence),durability_guaranteed=false}));
                using(FileStream file=new FileStream(marker,FileMode.CreateNew,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)){file.Write(bytes,0,bytes.Length);file.Flush(true);}return true;
            }catch(Exception){return false;}
        }
        public void Dispose() { lock (gate) { if (!closed) { closed = true; stream.Dispose(); } } }
    }

    public sealed class ApplicationLease : IDisposable {
        private readonly Mutex mutex;
        private readonly int ownerThread=Thread.CurrentThread.ManagedThreadId;
        private bool owned;
        public ApplicationLease() : this(TimingSafety.UserKernelName("gui"),CheckLegacy) { }
        internal ApplicationLease(string name, Action checkLegacy) {
            mutex = new Mutex(false, name);
            try { try { owned = mutex.WaitOne(0); } catch (AbandonedMutexException) { owned = true; } if (!owned) throw new InvalidOperationException("Another timing-clock instance is already open. Close it first."); }
            catch { mutex.Dispose(); throw; }
            try { checkLegacy(); } catch { Dispose(); throw; }
        }
        private static void CheckLegacy() {
                int me = Process.GetCurrentProcess().Id, session = Process.GetCurrentProcess().SessionId;
                foreach (Process p in Process.GetProcessesByName("BrsManualClock")) using (p) {
                    if (p.Id != me && p.SessionId == session) throw new InvalidOperationException("A same-session process named BrsManualClock is open (PID " + p.Id + "). Close it before opening this version; this conservative check does not establish its identity.");
                }
        }
        public void Dispose() {
            if(!owned)return;
            if(Thread.CurrentThread.ManagedThreadId!=ownerThread)throw new InvalidOperationException("GUI mutex must be released on its owning thread.");
            mutex.ReleaseMutex();owned=false;mutex.Dispose();
        }
    }
    internal sealed class PlanLease : IDisposable {
        private readonly Semaphore semaphore;
        private readonly Action release,close;
        private int owned;
        public PlanLease(string name = null) {
            semaphore = new Semaphore(1, 1, name ?? TimingSafety.UserKernelName("click-plan"));
            release=delegate{semaphore.Release();};close=semaphore.Dispose;
            try { if (!semaphore.WaitOne(0)) throw new InvalidOperationException("Another click is already scheduled in this session.");owned=1; }
            catch { semaphore.Dispose();throw; }
        }
        internal PlanLease(Action releaseAction,Action closeAction) {
            if(releaseAction==null||closeAction==null)throw new ArgumentNullException();release=releaseAction;close=closeAction;owned=1;
        }
        public void Dispose() {
            if(Interlocked.Exchange(ref owned,0)!=1)return;
            Exception a=null,b=null;try{release();}catch(Exception error){a=error;}try{close();}catch(Exception error){b=error;}
            if(a!=null&&b!=null)throw new AggregateException(a,b);if(a!=null)ExceptionDispatchInfo.Capture(a).Throw();if(b!=null)ExceptionDispatchInfo.Capture(b).Throw();
        }
    }

    public sealed class TargetSnapshot {
        public DesktopInput.Point Point;
        public IntPtr Root, Leaf, Monitor;
        public uint ProcessId, RootProcessId, ThreadId, RootThreadId, Dpi, LeafDpi;
        public long ProcessCreated, RootProcessCreated;
        public Rectangle RootBounds, LeafBounds, ClientBounds, DesktopBounds;
        public string DisplayLayout, DesktopName, ProcessName, RootClass, LeafClass;
        public int QuietMilliseconds;
        public bool MouseButtonsSwapped;
        internal TargetSnapshot Copy() { return (TargetSnapshot)MemberwiseClone(); }
        public string Difference(TargetSnapshot other) {
            if (other == null) return "target_unavailable";
            if (Point.X != other.Point.X || Point.Y != other.Point.Y) return "point_changed";
            if (Root != other.Root || Leaf != other.Leaf || ProcessId != other.ProcessId || RootProcessId != other.RootProcessId || ThreadId != other.ThreadId || RootThreadId != other.RootThreadId || ProcessCreated != other.ProcessCreated || RootProcessCreated != other.RootProcessCreated) return "target_changed";
            if (RootClass != other.RootClass || LeafClass != other.LeafClass) return "target_changed";
            if (RootBounds != other.RootBounds || LeafBounds != other.LeafBounds || ClientBounds != other.ClientBounds) return "target_moved";
            if (Dpi != other.Dpi || LeafDpi != other.LeafDpi || Monitor!=other.Monitor || DesktopBounds != other.DesktopBounds || DisplayLayout != other.DisplayLayout) return "desktop_changed";
            if (DesktopName != other.DesktopName) return "desktop_inactive";
            if (MouseButtonsSwapped != other.MouseButtonsSwapped || QuietMilliseconds != other.QuietMilliseconds) return "mouse_settings_changed";
            return null;
        }
    }
    public sealed class TargetGuardException : InvalidOperationException {
        public readonly string Reason;
        public readonly int? NativeCode;
        public TargetGuardException(string reason,int? nativeCode=null) : base(reason) { Reason = reason;NativeCode=nativeCode; }
    }
    public static class TargetGuard {
        // A process cannot move to another logon session during its lifetime.
        private static readonly int CurrentSession=Process.GetCurrentProcess().SessionId;
        [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; public Rectangle Bounds { get { return Rectangle.FromLTRB(Left, Top, Right, Bottom); } } }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct MonitorInfo { public int Size; public Rect Monitor, Work; public uint Flags; [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Device; }
        [StructLayout(LayoutKind.Sequential)] private struct LastInput { public uint Size, Tick; }
        [StructLayout(LayoutKind.Sequential)] private struct ObjectFlags { public int Inherit, Reserved; public uint Flags; }
        private delegate bool MonitorCallback(IntPtr monitor, IntPtr dc, ref Rect rect, IntPtr data);
        [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(DesktopInput.Point point);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr window);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window,StringBuilder name,int count);
        [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Rect rect);
        [DllImport("user32.dll")] private static extern bool GetClientRect(IntPtr window, out Rect rect);
        [DllImport("user32.dll")] private static extern bool ClientToScreen(IntPtr window, ref DesktopInput.Point point);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
        [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
        [DllImport("user32.dll")] private static extern IntPtr MonitorFromPoint(DesktopInput.Point point, uint flags);
        [DllImport("user32.dll")] private static extern bool EnumDisplayMonitors(IntPtr dc, IntPtr clip, MonitorCallback callback, IntPtr data);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
        [DllImport("user32.dll")] private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
        [DllImport("user32.dll")] private static extern bool CloseDesktop(IntPtr desktop);
        [DllImport("user32.dll")] private static extern IntPtr GetThreadDesktop(uint thread);
        [DllImport("user32.dll")] private static extern IntPtr GetProcessWindowStation();
        [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "GetUserObjectInformationW")] private static extern bool ObjectName(IntPtr handle, int index, StringBuilder buffer, uint length, out uint needed);
        [DllImport("user32.dll", EntryPoint = "GetUserObjectInformationW")] private static extern bool StationFlags(IntPtr handle, int index, out ObjectFlags flags, uint length, out uint needed);
        [DllImport("user32.dll")] private static extern uint GetDoubleClickTime();
        [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LastInput info);
        [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
        [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint process);
        [DllImport("kernel32.dll")] private static extern bool GetProcessTimes(IntPtr process, out long created, out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("wtsapi32.dll")] private static extern bool WTSQuerySessionInformation(IntPtr server, int session, int infoClass, out IntPtr data, out uint length);
        [DllImport("wtsapi32.dll")] private static extern void WTSFreeMemory(IntPtr memory);
        [DllImport("user32.dll", SetLastError = true)] public static extern bool RegisterHotKey(IntPtr window, int id, uint modifiers, uint key);
        [DllImport("user32.dll")] public static extern bool UnregisterHotKey(IntPtr window, int id);

        private static string NameOf(IntPtr handle) { uint needed; StringBuilder name = new StringBuilder(256); if (handle == IntPtr.Zero || !ObjectName(handle, 2, name, 512, out needed)) throw new TargetGuardException("desktop_state_unknown"); return name.ToString(); }
        private static string ClassOf(IntPtr handle) { StringBuilder name=new StringBuilder(256);if(GetClassName(handle,name,name.Capacity)==0)throw new TargetGuardException("target_identity_unknown");return name.ToString(); }
        public static string ActiveDesktop() {
            IntPtr data; uint length;
            if (!WTSQuerySessionInformation(IntPtr.Zero, CurrentSession, 8, out data, out length)) throw new TargetGuardException("session_state_unknown");
            try { if (length < 4 || Marshal.ReadInt32(data) != 0) throw new TargetGuardException("session_inactive"); } finally { WTSFreeMemory(data); }
            ObjectFlags station; uint needed;
            if (!StationFlags(GetProcessWindowStation(), 1, out station, 12, out needed) || (station.Flags & 1) == 0) throw new TargetGuardException("desktop_inactive");
            IntPtr input = OpenInputDesktop(0, false, 1);
            if (input == IntPtr.Zero) throw new TargetGuardException("desktop_inactive");
            try {
                string name = NameOf(input);
                if (name != NameOf(GetThreadDesktop(GetCurrentThreadId()))) throw new TargetGuardException("desktop_inactive");
                return name;
            } finally { CloseDesktop(input); }
        }
        public static uint LastInputTick() { LastInput input = new LastInput { Size = (uint)Marshal.SizeOf(typeof(LastInput)) }; if (!GetLastInputInfo(ref input)) throw new TargetGuardException("input_state_unknown"); return input.Tick; }
        private static string Layout() {
            List<string> parts = new List<string>(); bool failed = false;
            MonitorCallback callback = delegate(IntPtr monitor, IntPtr dc, ref Rect rect, IntPtr state) {
                MonitorInfo info = new MonitorInfo { Size = Marshal.SizeOf(typeof(MonitorInfo)) };
                if (!GetMonitorInfo(monitor, ref info)) { failed = true; return false; }
                parts.Add(info.Device + ":" + monitor.ToInt64().ToString("x", CultureInfo.InvariantCulture) + ":" + info.Monitor.Bounds.ToString() + ":" + info.Flags);
                return true;
            };
            if (!EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, callback, IntPtr.Zero) || failed || parts.Count == 0) throw new TargetGuardException("display_state_unknown");
            parts.Sort(StringComparer.Ordinal); return String.Join("|", parts.ToArray());
        }
        private static long CreationTime(uint process) {
            IntPtr handle=OpenProcess(0x1000,false,process); long created,exit,kernel,user;
            if (handle==IntPtr.Zero) throw new TargetGuardException("target_identity_unknown");
            try { if (!GetProcessTimes(handle,out created,out exit,out kernel,out user)) throw new TargetGuardException("target_identity_unknown"); return created; } finally { CloseHandle(handle); }
        }
        public static TargetSnapshot Capture(DesktopInput.Point point, IntPtr forbiddenRoot) {
            return Capture(point,forbiddenRoot,true);
        }
        internal static TargetSnapshot CaptureForTest(DesktopInput.Point point,IntPtr forbiddenRoot,Action beforeFinalRead) { return Capture(point,forbiddenRoot,true,beforeFinalRead); }
        internal static TargetSnapshot CaptureDesktopForTest(DesktopInput.Point point,Func<string> readDesktop) {return Capture(point,IntPtr.Zero,false,null,readDesktop);}
        private static TargetSnapshot Capture(DesktopInput.Point point, IntPtr forbiddenRoot,bool includeDisplayName,Action beforeFinalRead=null,Func<string> testDesktop=null) {
            string desktop = testDesktop==null?ActiveDesktop():testDesktop();
            Rectangle bounds = DesktopInput.VirtualDesktop();
            IntPtr monitor=MonitorFromPoint(point,0);
            if (bounds.Width > 65536 || bounds.Height > 65536 || !bounds.Contains(point.X, point.Y) || monitor == IntPtr.Zero) throw new TargetGuardException("point_outside_monitor");
            IntPtr leaf=WindowFromPoint(point),root=DesktopInput.RootOf(leaf);
            if (root == IntPtr.Zero || leaf == IntPtr.Zero || root == forbiddenRoot || !IsWindowVisible(root) || !IsWindowEnabled(root) || !IsWindowVisible(leaf) || !IsWindowEnabled(leaf) || IsIconic(root)) throw new TargetGuardException("target_unavailable");
            uint process, rootProcess; uint thread = GetWindowThreadProcessId(leaf, out process), rootThread = GetWindowThreadProcessId(root, out rootProcess);
            if (thread == 0 || rootThread == 0 || process == 0 || rootProcess==0) throw new TargetGuardException("target_identity_unknown");
            long created=CreationTime(process),rootCreated=rootProcess==process?created:CreationTime(rootProcess);
            string rootClass=ClassOf(root),leafClass=ClassOf(leaf);
            Rect rootRect, leafRect, clientRect; DesktopInput.Point origin = new DesktopInput.Point();
            if (!GetWindowRect(root, out rootRect) || !GetWindowRect(leaf, out leafRect) || !GetClientRect(root, out clientRect) || !ClientToScreen(root, ref origin)) throw new TargetGuardException("target_geometry_unknown");
            uint dpi = GetDpiForWindow(root),leafDpi=GetDpiForWindow(leaf); if (dpi == 0 || leafDpi==0) throw new TargetGuardException("target_dpi_unknown");
            bool swapped = GetSystemMetrics(23) != 0;
            if (swapped) throw new TargetGuardException("swapped_mouse_buttons_unsupported");
            string processName=null;if(includeDisplayName)using (Process owner = Process.GetProcessById((int)process)) processName = owner.ProcessName;
            TargetSnapshot result = new TargetSnapshot { Point = point, Root = root, Leaf = leaf, ProcessId = process,RootProcessId=rootProcess, ThreadId = thread, RootThreadId = rootThread, ProcessCreated = created,RootProcessCreated=rootCreated, Dpi = dpi,LeafDpi=leafDpi, RootBounds = rootRect.Bounds, LeafBounds = leafRect.Bounds, ClientBounds = new Rectangle(origin.X, origin.Y, clientRect.Right - clientRect.Left, clientRect.Bottom - clientRect.Top), DesktopBounds = bounds, DisplayLayout = Layout(), DesktopName = desktop, ProcessName = processName, MouseButtonsSwapped = swapped, QuietMilliseconds = checked((int)GetDoubleClickTime() + 50) };
            result.RootClass=rootClass;result.LeafClass=leafClass;result.Monitor=monitor;
            if(beforeFinalRead!=null)beforeFinalRead(); // Test-only overload; all stability reads follow it.
            // ProcessName is display-only; PID + creation time and window classes
            // are guards. Titles/content are not collected or semantic identity.
            // Re-read identity after collecting geometry; a changing target is not accepted.
            uint finalProcess,finalRootProcess;
            Rect finalRootRect,finalLeafRect,finalClient;DesktopInput.Point finalOrigin=new DesktopInput.Point();IntPtr finalLeaf=WindowFromPoint(point);
            if (finalLeaf!=leaf || DesktopInput.RootOf(finalLeaf) != root || GetWindowThreadProcessId(leaf,out finalProcess)!=thread || finalProcess!=process || GetWindowThreadProcessId(root,out finalRootProcess)!=rootThread || finalRootProcess!=rootProcess || !GetWindowRect(root,out finalRootRect) || !GetWindowRect(leaf,out finalLeafRect) || finalRootRect.Bounds!=result.RootBounds || finalLeafRect.Bounds!=result.LeafBounds) throw new TargetGuardException("target_changed");
            if (ClassOf(root)!=rootClass || ClassOf(leaf)!=leafClass || CreationTime(process)!=created || (rootProcess!=process && CreationTime(rootProcess)!=rootCreated) || !IsWindowVisible(root) || !IsWindowEnabled(root) || !IsWindowVisible(leaf) || !IsWindowEnabled(leaf) || IsIconic(root)) throw new TargetGuardException("target_changed");
            if(!GetClientRect(root,out finalClient)||!ClientToScreen(root,ref finalOrigin)||new Rectangle(finalOrigin.X,finalOrigin.Y,finalClient.Right-finalClient.Left,finalClient.Bottom-finalClient.Top)!=result.ClientBounds)throw new TargetGuardException("target_moved");
            if(GetDpiForWindow(root)!=dpi||GetDpiForWindow(leaf)!=leafDpi||DesktopInput.VirtualDesktop()!=bounds||MonitorFromPoint(point,0)!=monitor||Layout()!=result.DisplayLayout)throw new TargetGuardException("desktop_changed");
            if((GetSystemMetrics(23)!=0)!=swapped||checked((int)GetDoubleClickTime()+50)!=result.QuietMilliseconds)throw new TargetGuardException("mouse_settings_changed");
            string finalDesktop=testDesktop==null?ActiveDesktop():testDesktop();
            if(!String.Equals(finalDesktop,desktop,StringComparison.Ordinal))throw new TargetGuardException("desktop_inactive");
            return result;
        }
        // A short, owned recheck supplements the full stable snapshot. It does
        // not replace it and cannot remove the last native check-to-call race.
        internal static string ValidateNearInput(TargetSnapshot expected) {
            return Validate(expected);
        }
        public static string Validate(TargetSnapshot expected) {
            try { return expected.Difference(Capture(expected.Point, IntPtr.Zero,false)); }
            catch (TargetGuardException ex) { return ex.Reason; }
        }
    }
    internal sealed class InputActivity {
        private bool initialized;
        private uint last;
        private double changedAt;
        public void Observe(uint tick, double mono) { if (!initialized || tick != last) { initialized = true; last = tick; changedAt = mono; } }
        public bool Quiet(uint currentTick, double mono, int milliseconds) { uint age=unchecked(currentTick-last);return initialized && age<=Int32.MaxValue && age>=milliseconds && mono-changedAt>=milliseconds; }
    }
}
