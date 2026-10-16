param([Parameter(Mandatory)][int64]$Hwnd)
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public static class D {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int l, t, r, b; }
  [StructLayout(LayoutKind.Sequential)] public struct GUITHREADINFO {
    public int cbSize; public int flags; public IntPtr hwndActive, hwndFocus, hwndCapture,
    hwndMenuOwner, hwndMoveSize, hwndCaret; public RECT rcCaret; }
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool GetGUIThreadInfo(uint tid, ref GUITHREADINFO gi);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string cls, string title);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  public static string Cls(IntPtr h) { var s = new StringBuilder(256); GetClassName(h, s, 256); return s.ToString(); }
  public static string Txt(IntPtr h) { var s = new StringBuilder(256); GetWindowText(h, s, 256); return s.ToString(); }
  public static List<IntPtr> Kids(IntPtr p) { var r = new List<IntPtr>();
    EnumChildWindows(p, (h, l) => { if (GetParent(h) == p) r.Add(h); return true; }, IntPtr.Zero); return r; }
}
"@
$h = [IntPtr]$Hwnd
$procId = 0; $tid = [D]::GetWindowThreadProcessId($h, [ref]$procId)
$gi = New-Object D+GUITHREADINFO; $gi.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($gi)
[void][D]::GetGUIThreadInfo($tid, [ref]$gi)
"focus:  $($gi.hwndFocus) class=$([D]::Cls($gi.hwndFocus)) text=$([D]::Txt($gi.hwndFocus))"
"active: $($gi.hwndActive) class=$([D]::Cls($gi.hwndActive))"
# Walk focus up to the frame
$w = $gi.hwndFocus; $chain = @()
while ($w -ne [IntPtr]::Zero -and $w -ne $h) { $chain += "$([D]::Cls($w))"; $w = [D]::GetParent($w) }
"focus chain: $($chain -join ' <- ')"
$mdi = [D]::FindWindowEx($h, [IntPtr]::Zero, "MDIClient", $null)
"MDIClient: $mdi"
if ($mdi -ne [IntPtr]::Zero) {
  foreach ($k in [D]::Kids($mdi)) { "  child $k vis=$([D]::IsWindowVisible($k)) class=$([D]::Cls($k)) text=$([D]::Txt($k))" }
}
