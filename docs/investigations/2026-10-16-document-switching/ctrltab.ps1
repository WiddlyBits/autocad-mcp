param([Parameter(Mandatory)][int64]$Hwnd, [Parameter(Mandatory)][string]$Expect, [int]$MaxPress = 4)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class W {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
"@
$h = [IntPtr]$Hwnd
$procId = 0; [void][W]::GetWindowThreadProcessId($h, [ref]$procId)
$p = Get-Process -Id $procId -ErrorAction SilentlyContinue
"target: hwnd=$Hwnd pid=$procId proc=$($p.ProcessName) title=$($p.MainWindowTitle)"
if (-not $p -or $p.ProcessName -notmatch '^acad') { "ABORT: hwnd is not an AutoCAD process"; exit 2 }

if ([W]::IsIconic($h)) { [void][W]::ShowWindow($h, 9) }
if ([W]::GetForegroundWindow() -eq $h) {
  "AutoCAD already foreground - no Alt tap"
} else {
  # A bare Alt tap releases the foreground lock so SetForegroundWindow is honoured.
  # Only while another app is foreground: in AutoCAD a bare Alt opens ribbon keytips.
  "AutoCAD not foreground - Alt tap + SetForegroundWindow"
  [W]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [W]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
  [void][W]::SetForegroundWindow($h)
  Start-Sleep -Milliseconds 400
}

for ($n = 1; $n -le $MaxPress; $n++) {
  $fg = [W]::GetForegroundWindow()
  if ($fg -ne $h) { "ABORT: foreground is $fg, not $Hwnd - no keystroke sent"; exit 3 }
  [W]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)   # Ctrl down
  [W]::keybd_event(0x09, 0, 0, [UIntPtr]::Zero)   # Tab down
  [W]::keybd_event(0x09, 0, 2, [UIntPtr]::Zero)   # Tab up
  [W]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)   # Ctrl up
  Start-Sleep -Milliseconds 700
  $t = (Get-Process -Id $procId).MainWindowTitle
  "press $n -> $t"
  if ($t -like "*$Expect*") { "MATCH"; exit 0 }
}
"NO MATCH after $MaxPress presses"; exit 4
