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

  skills\** is pinned to LF in .gitattributes, so a byte comparison is valid for
  every file except SKILL.md, whose frontmatter claude.ai rewrites on upload.

.PARAMETER Check
  Compare every loaded copy with the repo. SKILL.md: name and description as
  values, body after the frontmatter byte for byte. Every other file, including
  references\: byte for byte. Each skill must be in the manifest; an updatedAt
  older than that skill's own last commit is a warning, not drift. Exit 1 on
  any drift. Copies nothing.

.PARAMETER SyncedRoot
  Parent of the <guid>_<guid> folders. Defaults to ~\.claude\skills\synced.
  Point it at a scratch copy to test -Check without touching the live folder.

.PARAMETER Package
  Write dist\<skill>.zip for each skill, rooted at the skill folder, for the
  claude.ai upload. Copies nothing to the loaded folders.

.PARAMETER WhatIf
  With no switch: report what would be copied without writing anything.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Check,
    [switch]$Package,
    [string]$SyncedRoot = (Join-Path $HOME '.claude\skills\synced')
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

# claude.ai rewrites SKILL.md frontmatter on upload: the repo's folded
# `description: >-` comes back as a one-line plain or single-quoted scalar. So
# SKILL.md is compared as parsed values (name, description) plus the exact body
# after the closing ---, never as a file hash.
function Read-SkillMd($path) {
    $text = [IO.File]::ReadAllText($path) -replace "`r`n", "`n"
    $m = [regex]::Match($text, '\A---\n(.*?)\n---\n(.*)\z', 'Singleline')
    if (-not $m.Success) { return $null }
    $fields = [ordered]@{}
    $key = $null
    foreach ($line in $m.Groups[1].Value -split "`n") {
        if ($line -match '^([A-Za-z_][\w-]*):\s*(.*)$') {
            $key = $Matches[1]
            $fields[$key] = @($Matches[2])
        } elseif ($key -and $line -match '^\s+\S') {
            $fields[$key] += $line.Trim()
        }
    }
    $values = @{}
    foreach ($k in $fields.Keys) {
        $parts = @($fields[$k])
        # Folded (>, >-) or plain multi-line: continuation lines join with one space.
        if ($parts[0] -match '^[>|][-+]?$') { $parts = @($parts | Select-Object -Skip 1) }
        $v = ($parts -join ' ').Trim()
        if ($v -match "^'(.*)'$") { $v = $Matches[1] -replace "''", "'" }
        elseif ($v -match '^"(.*)"$') { $v = $Matches[1] -replace '\\"', '"' }
        $values[$k] = $v
    }
    [pscustomobject]@{ Fields = $values; Body = $m.Groups[2].Value }
}

function Compare-SkillMd($want, $have) {
    $a = Read-SkillMd $want
    $b = Read-SkillMd $have
    if (-not $a) { return @('SKILL.md: repo frontmatter unparseable') }
    if (-not $b) { return @('SKILL.md: loaded frontmatter unparseable') }
    $out = @()
    foreach ($k in 'name', 'description') {
        if ($a.Fields[$k] -cne $b.Fields[$k]) { $out += "differs: SKILL.md $k" }
    }
    if ($a.Body -cne $b.Body) { $out += 'differs: SKILL.md body' }
    $out
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

$syncedPattern = Join-Path $SyncedRoot '*'
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
    $warned = 0
    # Per skill: the newest commit to any skill must not age the other two.
    $lastCommit = @{}
    foreach ($skill in $skills) {
        $c = (& git -C $repo log -1 --format=%cI -- "skills/$($skill.Name)") | Out-String
        $lastCommit[$skill.Name] = [DateTimeOffset]::Parse($c.Trim())
        Write-Host "Last commit touching skills\$($skill.Name): $($lastCommit[$skill.Name].ToString('u'))"
    }
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
                    if ($rel -eq 'SKILL.md') {
                        $problems += @(Compare-SkillMd (Join-Path $skill.FullName $rel) (Join-Path $dest $rel))
                        continue
                    }
                    $a = (Get-FileHash (Join-Path $skill.FullName $rel)).Hash
                    $b = (Get-FileHash (Join-Path $dest $rel)).Hash
                    if ($a -ne $b) { $problems += "differs: $rel" }
                }
            }
            # Content is the evidence; updatedAt only approximates it. An upload
            # built from the same content just before the commit is delivered, so
            # an old updatedAt is a warning, not drift.
            $warnings = @()
            $entry = @($manifest.skills | Where-Object { $_.name -eq $skill.Name })
            if ($entry.Count -eq 0) {
                $problems += 'not in manifest'
            } elseif ([DateTimeOffset]::Parse($entry[0].updatedAt) -lt $lastCommit[$skill.Name]) {
                $warnings += "manifest updatedAt $($entry[0].updatedAt) predates the last commit to skills\$($skill.Name)"
            }
            if ($problems) {
                $drift++
                Write-Host "   DRIFT  $($skill.Name)"
                ($problems + $warnings) | ForEach-Object { Write-Host "            $_" }
            } elseif ($warnings) {
                $warned++
                Write-Host "   warn   $($skill.Name)"
                $warnings | ForEach-Object { Write-Host "            $_" }
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
    if ($warned) { Write-Host "$warned skill/location pair(s) match but carry an old updatedAt." }
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
