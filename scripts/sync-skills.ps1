<#
.SYNOPSIS
  Preview, check, or package the repo's skills.

.DESCRIPTION
  The repo's skills\ folder is the only source of truth. Every other copy is a
  build artifact:

    ~\.claude\skills\synced\<guid>_<guid>\   (dirs holding a manifest.json)
        Written by the claude.ai skill sync. Copying into it here is a LOCAL
        PREVIEW: the next sync round overwrites it with whatever claude.ai holds.
    claude.ai -> Customize -> Skills
        Delivery. Gianni uploads the -Package zips there after a merge to main.

  Editing a loaded copy is how the skill drifted ahead of the code on
  2026-08-22: it documented a preflight block and an mcp_select.lsp that existed
  only on an unmerged branch, and nothing could catch it because one half was
  not under version control. -Check is what catches it now.

  skills\** is pinned to LF in .gitattributes, so a byte comparison is valid.

.PARAMETER Check
  Compare every loaded copy with the repo, byte for byte, including
  references\. Also check that each repo skill is in the manifest with an
  updatedAt at or after the last commit touching skills\. Exit 1 on any drift.
  Copies nothing.

.PARAMETER Package
  Write dist\<skill>.zip for each skill, rooted at the skill folder, for the
  claude.ai upload. Copies nothing to the loaded folders.

.PARAMETER WhatIf
  With no switch: report what would be copied without writing anything.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Check,
    [switch]$Package
)

$ErrorActionPreference = 'Stop'

$repo = Join-Path $PSScriptRoot '..' | Resolve-Path -ErrorAction Stop
$source = Join-Path $repo 'skills' | Resolve-Path -ErrorAction Stop
$skills = @(Get-ChildItem -Path $source -Directory)
if ($skills.Count -eq 0) { Write-Error "No skills in $source" }

function Get-RelativeFiles($root) {
    $prefix = $root.TrimEnd('\') + '\'
    Get-ChildItem -Path $root -Recurse -File |
        ForEach-Object { $_.FullName.Substring($prefix.Length) } |
        Sort-Object
}

if ($Package) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $dist = Join-Path $repo 'dist'
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    foreach ($skill in $skills) {
        $zipPath = Join-Path $dist "$($skill.Name).zip"
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
        $zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
        try {
            foreach ($rel in Get-RelativeFiles $skill.FullName) {
                # Forward slashes: a backslash entry name unpacks as one flat filename off Windows.
                $entry = "$($skill.Name)/" + ($rel -replace '\\', '/')
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $zip, (Join-Path $skill.FullName $rel), $entry, 'Optimal') | Out-Null
            }
        } finally { $zip.Dispose() }
        $n = @(Get-RelativeFiles $skill.FullName).Count
        Write-Host "   $zipPath  ($n files)"
    }
    Write-Host ""
    Write-Host "Packaged $($skills.Count) skill(s) into $dist."
    Write-Host "Upload: claude.ai -> Customize -> Skills, then run -Check."
    return
}

$syncedPattern = Join-Path $HOME '.claude\skills\synced\*'
$targets = @(Get-ChildItem -Path $syncedPattern -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName 'manifest.json') })

if ($targets.Count -eq 0) {
    Write-Error @"
No loaded skills directory found under
  $syncedPattern (with a manifest.json)
The GUID path may have been re-provisioned, or Claude has not created it yet.
"@
}

if ($Check) {
    $drift = 0
    $lastCommit = (& git -C $repo log -1 --format=%cI -- skills) | Out-String
    $lastCommit = [DateTimeOffset]::Parse($lastCommit.Trim())
    Write-Host "Last commit touching skills\: $($lastCommit.ToString('u'))"
    foreach ($target in $targets) {
        Write-Host "-> $($target.FullName)"
        $manifest = Get-Content (Join-Path $target.FullName 'manifest.json') -Raw -Encoding UTF8 |
            ConvertFrom-Json
        foreach ($skill in $skills) {
            $problems = @()
            $dest = Join-Path $target.FullName $skill.Name
            if (-not (Test-Path $dest)) {
                $problems += 'not loaded'
            } else {
                $want = @(Get-RelativeFiles $skill.FullName)
                $have = @(Get-RelativeFiles $dest)
                $missing = @($want | Where-Object { $have -notcontains $_ })
                $extra = @($have | Where-Object { $want -notcontains $_ })
                if ($missing) { $problems += "missing: $($missing -join ', ')" }
                if ($extra) { $problems += "extra: $($extra -join ', ')" }
                foreach ($rel in ($want | Where-Object { $have -contains $_ })) {
                    $a = (Get-FileHash (Join-Path $skill.FullName $rel)).Hash
                    $b = (Get-FileHash (Join-Path $dest $rel)).Hash
                    if ($a -ne $b) { $problems += "differs: $rel" }
                }
            }
            $entry = @($manifest.skills | Where-Object { $_.name -eq $skill.Name })
            if ($entry.Count -eq 0) {
                $problems += 'not in manifest'
            } elseif ([DateTimeOffset]::Parse($entry[0].updatedAt) -lt $lastCommit) {
                $problems += "manifest updatedAt $($entry[0].updatedAt) predates the last skills\ commit"
            }
            if ($problems) {
                $drift++
                Write-Host "   DRIFT  $($skill.Name)"
                $problems | ForEach-Object { Write-Host "            $_" }
            } else {
                Write-Host "   ok     $($skill.Name)"
            }
        }
    }
    Write-Host ""
    if ($drift) {
        Write-Host "$drift skill/location pair(s) drifted."
        exit 1
    }
    Write-Host "No drift."
    exit 0
}

foreach ($target in $targets) {
    Write-Host "-> $($target.FullName)  (local preview; the next sync round overwrites it)"
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
