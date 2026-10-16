param([Parameter(Mandatory)][int64]$Hwnd, [Parameter(Mandatory)][string]$Expect)
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public static class M {
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr after, string cls, string title);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  public static string Txt(IntPtr h) { var s = new StringBuilder(256); GetWindowText(h, s, 256); return s.ToString(); }
  public static List<IntPtr> Kids(IntPtr p) { var r = new List<IntPtr>();
    EnumChildWindows(p, (h, l) => { if (GetParent(h) == p) r.Add(h); return true; }, IntPtr.Zero); return r; }
}
"@
$WM_MDIACTIVATE = 0x0222; $WM_MDIGETACTIVE = 0x0229
$h = [IntPtr]$Hwnd
$mdi = [M]::FindWindowEx($h, [IntPtr]::Zero, "MDIClient", $null)
if ($mdi -eq [IntPtr]::Zero) { "ABORT: no MDIClient"; exit 2 }
# Match the child by exact title, never by stale handle
$target = [M]::Kids($mdi) | Where-Object { [M]::Txt($_) -eq $Expect }
if (@($target).Count -ne 1) { "ABORT: $(@($target).Count) children titled '$Expect'"; exit 3 }
"before: active child = $([M]::Txt([M]::SendMessage($mdi, $WM_MDIGETACTIVE, [IntPtr]::Zero, [IntPtr]::Zero)))"
[void][M]::SendMessage($mdi, $WM_MDIACTIVATE, $target, [IntPtr]::Zero)
Start-Sleep -Milliseconds 700
"after:  active child = $([M]::Txt([M]::SendMessage($mdi, $WM_MDIGETACTIVE, [IntPtr]::Zero, [IntPtr]::Zero)))"
"frame title = $((Get-Process -Id ((Get-Process acadlt).Id)).MainWindowTitle)"
