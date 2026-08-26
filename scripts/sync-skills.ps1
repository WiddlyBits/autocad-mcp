<#
.SYNOPSIS
  Deploy the repo's skills into the Claude skills directory.

.DESCRIPTION
  The canonical copy of these skills is here, in git, next to the MCP server
  they document. The live copy lives under a session-scoped AppData path whose
  GUIDs the app owns and can re-provision:

    %APPDATA%\Claude\local-agent-mode-sessions\skills-plugin\<guid>\<guid>\skills\

  That directory is a deployment target, not a source. Editing it in place is
  how the skill drifted ahead of the code on 2026-08-22: it documented a
  preflight block and an mcp_select.lsp that existed only on an unmerged
  branch, and nothing could catch the divergence because one half was not
  under version control.

  The GUIDs are globbed rather than hardcoded, because hardcoding them is the
  same bet that failed. Every matching skills root is synced; a stale one
  receiving a current copy is harmless, a missed live one is not.

.PARAMETER WhatIf
  Report what would be copied without writing anything.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot '..\skills' | Resolve-Path -ErrorAction Stop
$pattern = Join-Path $env:APPDATA 'Claude\local-agent-mode-sessions\skills-plugin\*\*\skills'
$targets = @(Get-ChildItem -Path $pattern -Directory -ErrorAction SilentlyContinue)

if ($targets.Count -eq 0) {
    Write-Error @"
No skills directory found under:
  $pattern
The GUID path may have been re-provisioned, or Claude has not created it yet.
Nothing was copied.
"@
}

$skills = @(Get-ChildItem -Path $source -Directory)
if ($skills.Count -eq 0) { Write-Error "No skills to sync in $source" }

foreach ($target in $targets) {
    Write-Host "-> $($target.FullName)"
    foreach ($skill in $skills) {
        $dest = Join-Path $target.FullName $skill.Name
        if ($PSCmdlet.ShouldProcess($dest, "sync $($skill.Name)")) {
            # Remove first: a reference file deleted in git must not survive here.
            if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
            Copy-Item $skill.FullName $dest -Recurse -Force
        }
        $n = (Get-ChildItem $skill.FullName -Recurse -File).Count
        Write-Host "   $($skill.Name)  ($n files)"
    }
}

Write-Host ""
Write-Host "Synced $($skills.Count) skill(s) to $($targets.Count) location(s)."
Write-Host "Source of truth: $source"
