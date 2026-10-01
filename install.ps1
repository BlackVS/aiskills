<#
.SYNOPSIS
Install the review skills into a project (or user-level) skills directory.

.DESCRIPTION
PowerShell equivalent of install.sh.

Re-running replaces previously installed copies of the same skills (idempotent):
each selected skill directory is removed and copied again, supporting files
(references\, scripts\) included, so an upgrade is the same command again.
Skills not in the selected set are left untouched.

.PARAMETER Repo
Target repository directory. Required unless -User is given.

.PARAMETER Tool
Where to install. Repeatable (comma-separated). One of:
  claude    -> <repo>\.claude\skills      (Claude Code; also read by OpenCode)
  codex     -> <repo>\.agents\skills      (Codex CLI/IDE; also Gemini CLI, OpenCode)
  agents    -> the same directory as codex (older name, still accepted)
  opencode  -> <repo>\.opencode\skills
  openhands -> <repo>\.openhands\skills
  all       -> claude, codex, opencode, openhands
Default: claude,codex. Claude Code and Codex read different directories,
so a skill has to be copied to both.

.PARAMETER Skills
Comma-separated skill names, or "core" (default) or "all".
  core = architecture-review,oh-code-review,oh-technical-writing
Both -Skills a,b (PowerShell array) and -Skills 'a,b' (one string) work.

.PARAMETER Prompts
Also copy prompts\ to <repo>\.claude\prompts (or the tool's dir; for opencode the
commands dir, so each prompt is a /<name> command).
With -User, also to ~\.codex\prompts when ~\.codex exists (Codex custom
prompts: /prompts:<name> in the CLI) and to ~\.config\opencode\commands when
~\.config\opencode exists.

.PARAMETER AgentsMd
Also write the "Code review gates" block (agents\review-gates.md) into the agent
instructions, between markers, replacing any previous copy:
  project: <repo>\AGENTS.md (created if absent); <repo>\CLAUDE.md and
           <repo>\GEMINI.md get an "@AGENTS.md" import (each created with just
           that import if absent) so Claude Code and Gemini CLI read it.
  -User:   ~\.claude\CLAUDE.md (Claude Code), plus, each created if absent:
           ~\.config\opencode\AGENTS.md when ~\.config\opencode exists (OpenCode v2
           reads only AGENTS.md), ~\.codex\AGENTS.md when ~\.codex exists (Codex
           global instructions) and ~\.gemini\GEMINI.md when ~\.gemini exists
           (Gemini CLI).

.PARAMETER User
Install user-level instead of into a repo (~ is the user profile, $HOME):
  claude -> ~\.claude\skills, codex -> ~\.agents\skills,
  opencode -> ~\.config\opencode\skills, openhands -> ~\.openhands\skills
  (OpenCode's ~\.config\opencode is $env:XDG_CONFIG_HOME\opencode when that is set)

.PARAMETER DryRun
Show what would be done.

.EXAMPLE
.\install.ps1 C:\src\my-repo

.EXAMPLE
.\install.ps1 -User -Tool claude,agents -Skills all -Prompts
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Repo,

    [Alias('t')]
    [string[]]$Tool = @(),

    [Alias('s')]
    [string[]]$Skills = @('core'),

    [Alias('p')]
    [switch]$Prompts,

    [Alias('a')]
    [switch]$AgentsMd,

    [Alias('u')]
    [switch]$User,

    [Alias('n', 'WhatIfInstall')]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Src = Join-Path $Here 'skills'
$Core = 'architecture-review,oh-code-review,oh-technical-writing'

function Fail([string]$Message) {
    Write-Error "install.ps1: $Message" -ErrorAction Stop
}

# --- resolve tools ---
$ToolList = @($Tool | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
# supplied but empty ('' , ',' or @()) is refused; only an omitted -Tool gets the default set
if ($PSBoundParameters.ContainsKey('Tool') -and $ToolList.Count -eq 0) { Fail 'no tool selected (see Get-Help .\install.ps1)' }
if ($ToolList.Count -eq 0) { $ToolList = @('claude', 'codex') }
# normalize: the older name, "all", duplicates (codex and agents share a directory)
$KnownTools = @('claude', 'codex', 'opencode', 'openhands')
$Resolved = New-Object System.Collections.Generic.List[string]
foreach ($t in $ToolList) {
    $names = switch ($t) { 'all' { $KnownTools } 'agents' { @('codex') } default { @($t) } }
    foreach ($n in $names) {
        if ($KnownTools -notcontains $n) { Fail "unknown tool: $n (see Get-Help .\install.ps1)" }
        if ($Resolved -notcontains $n) { $Resolved.Add($n) }
    }
}
$ToolList = @($Resolved)
if ($ToolList.Count -eq 0) { Fail 'no tool selected (see Get-Help .\install.ps1)' }

# --- resolve target ---
if (-not $User) {
    if (-not $Repo) { Get-Help $PSCommandPath; exit 1 }
    if (-not (Test-Path $Repo -PathType Container)) { Fail "target is not a directory: $Repo" }
    $Repo = (Resolve-Path $Repo).Path
} elseif ($Repo) {
    Fail '-User takes no repo argument'
}

# --- resolve skill set ---
$Available = Get-ChildItem $Src -Directory | Select-Object -ExpandProperty Name
# PS parses an unquoted "-Skills a,b" as an array; flatten either form to one list
$SkillsFlat = @($Skills | ForEach-Object { $_ -split ',' } | Where-Object { $_ }) -join ','
switch ($SkillsFlat) {
    'core' { $SkillsFlat = $Core }
    'all'  { $SkillsFlat = $Available -join ',' }
}
$Want = @($SkillsFlat -split ',' | Where-Object { $_ })
foreach ($s in $Want) {
    if (-not (Test-Path (Join-Path $Src "$s\SKILL.md"))) {
        Fail "unknown skill '$s' (available: $($Available -join ' '))"
    }
}
# architecture-review references its two siblings by relative path
if ($Want -contains 'architecture-review') {
    foreach ($dep in 'oh-code-review', 'oh-technical-writing') {
        if ($Want -notcontains $dep) {
            Write-Host "note: architecture-review needs $dep; adding it"
            $Want += $dep
        }
    }
}

# OpenCode reads its config from $XDG_CONFIG_HOME\opencode, ~\.config\opencode when unset
$OpenCodeDir = Join-Path $(if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }) 'opencode'
function Get-DestDir([string]$ToolName) {
    if ($User) {
        switch ($ToolName) {
            'claude'    { Join-Path $HOME '.claude\skills' }
            'codex'     { Join-Path $HOME '.agents\skills' }
            'opencode'  { Join-Path $OpenCodeDir 'skills' }
            'openhands' { Join-Path $HOME '.openhands\skills' }
        }
    } else {
        switch ($ToolName) {
            'claude'    { Join-Path $Repo '.claude\skills' }
            'codex'     { Join-Path $Repo '.agents\skills' }
            'opencode'  { Join-Path $Repo '.opencode\skills' }
            'openhands' { Join-Path $Repo '.openhands\skills' }
        }
    }
}

function Invoke-Step([string]$Description, [scriptblock]$Action) {
    if ($DryRun) { Write-Host "  [dry-run] $Description" } else { & $Action }
}

$PDests = @()
foreach ($t in $ToolList) {
    $Dest = Get-DestDir $t
    Write-Host "==> ${t}: $Dest"
    Invoke-Step "mkdir $Dest" { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }
    foreach ($s in $Want) {
        $Target = Join-Path $Dest $s
        $Verb = if (Test-Path $Target) { 'replaced' } else { 'installed' }
        Invoke-Step "rm -rf $Target" {
            if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
        }
        Invoke-Step "copy $s -> $Target" { Copy-Item -Recurse (Join-Path $Src $s) $Target }
        Write-Host "    $Verb $s"
    }
    if ($Prompts) {
        $PDest = Join-Path (Split-Path $Dest -Parent) $(if ($t -eq 'opencode') { 'commands' } else { 'prompts' })
        Invoke-Step "mkdir $PDest" { New-Item -ItemType Directory -Force -Path $PDest | Out-Null }
        Invoke-Step "copy prompts\*.md -> $PDest" {
            Copy-Item (Join-Path $Here 'prompts\*.md') $PDest -Force
        }
        Write-Host "    prompts -> $PDest"; $PDests += $PDest
    }
}

# --- Codex extras (skills reach Codex via -Tool codex; prompts and global
# --- AGENTS.md live in ~\.codex, touched only when ~\.codex already exists) ---
if ($User -and $Prompts -and (Test-Path (Join-Path $HOME '.codex') -PathType Container)) {
    $CDest = Join-Path $HOME '.codex\prompts'
    Write-Host "==> codex: $CDest"
    Invoke-Step "mkdir $CDest" { New-Item -ItemType Directory -Force -Path $CDest | Out-Null }
    Invoke-Step "copy prompts\*.md -> $CDest" {
        Copy-Item (Join-Path $Here 'prompts\*.md') $CDest -Force
    }
    Write-Host "    prompts -> $CDest (Codex: /prompts:<name>)"; $PDests += $CDest
}

# --- OpenCode extras (skills reach OpenCode through .claude\skills and .agents\skills;
# --- its commands and global AGENTS.md live in ~\.config\opencode, touched only when it exists) ---
if ($User -and $Prompts -and (Test-Path $OpenCodeDir -PathType Container) -and ($ToolList -notcontains 'opencode')) {
    $ODest = Join-Path $OpenCodeDir 'commands'
    Write-Host "==> opencode: $ODest"
    Invoke-Step "mkdir $ODest" { New-Item -ItemType Directory -Force -Path $ODest | Out-Null }
    Invoke-Step "copy prompts\*.md -> $ODest" {
        Copy-Item (Join-Path $Here 'prompts\*.md') $ODest -Force
    }
    Write-Host "    prompts -> $ODest (OpenCode: /<name>)"; $PDests += $ODest
}

# --- managed "Code review gates" block in agent instruction files ---
$BlockStart = '<!-- aiskills:review-gates start (managed by install.sh, do not edit inside) -->'
$BlockEnd = '<!-- aiskills:review-gates end -->'
# Releases before 1.27.0 named the set ai-skills and marked the block that way: an old block is
# replaced like a current one, so an upgrade never leaves two.
$OldBlockStart = '<!-- ai-skills:review-gates start (managed by install.sh, do not edit inside) -->'
$OldBlockEnd = '<!-- ai-skills:review-gates end -->'
function Write-Block([string]$File) {
    if ($DryRun) { Write-Host "  [dry-run] write review-gates block into $File"; return }
    $dir = Split-Path $File -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lines = @()
    # -Encoding UTF8 everywhere: PS 5.1 Get-Content defaults to ANSI and
    # mangles multibyte characters (em-dashes became mojibake in the target).
    if (Test-Path $File) { $lines = @(Get-Content $File -Encoding UTF8) }
    $kept = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($l in $lines) {
        if ($l -eq $BlockStart -or $l -eq $OldBlockStart) { $skip = $true }
        if (-not $skip) { $kept.Add($l) }
        if ($l -eq $BlockEnd -or $l -eq $OldBlockEnd) { $skip = $false }
    }
    while ($kept.Count -gt 0 -and $kept[$kept.Count - 1].Trim() -eq '') { $kept.RemoveAt($kept.Count - 1) }
    if ($kept.Count -gt 0) { $kept.Add('') }
    $kept.Add($BlockStart)
    foreach ($l in Get-Content (Join-Path $Here 'agents\review-gates.md') -Encoding UTF8) { $kept.Add($l) }
    $kept.Add($BlockEnd)
    # LF endings, no BOM: the same file is read on Linux
    [System.IO.File]::WriteAllText($File, (($kept -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
}
function Ensure-Import([string]$RepoDir, [string]$Name) {
    # <repo>\<Name> gets "@AGENTS.md" so Claude Code (CLAUDE.md) or Gemini CLI (GEMINI.md) reads AGENTS.md
    $f = Join-Path $RepoDir $Name
    if ($DryRun) { Write-Host "  [dry-run] ensure @AGENTS.md import in $f"; return }
    if (Test-Path $f) {
        $content = Get-Content $f -Raw -Encoding UTF8
        if ($content -notmatch '(?m)^@AGENTS\.md\s*$') {
            if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) { $content += "`n" }
            [System.IO.File]::WriteAllText($f, $content + "`n@AGENTS.md`n", (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "    added @AGENTS.md import to $f"
        }
    } else {
        [System.IO.File]::WriteAllText($f, "@AGENTS.md`n", (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "    created $f with @AGENTS.md import"
    }
}
if ($AgentsMd) {
    Write-Host '==> agent instructions: review-gates block'
    if ($User) {
        $g = Join-Path $HOME '.claude\CLAUDE.md'; Write-Block $g; Write-Host "    block -> $g"
        $o = Join-Path $OpenCodeDir 'AGENTS.md'
        if (Test-Path $OpenCodeDir -PathType Container) { Write-Block $o; Write-Host "    block -> $o" }
        $c = Join-Path $HOME '.codex\AGENTS.md'
        if (Test-Path (Join-Path $HOME '.codex') -PathType Container) { Write-Block $c; Write-Host "    block -> $c" }
        $m = Join-Path $HOME '.gemini\GEMINI.md'
        if (Test-Path (Join-Path $HOME '.gemini') -PathType Container) { Write-Block $m; Write-Host "    block -> $m" }
    } else {
        $a = Join-Path $Repo 'AGENTS.md'; Write-Block $a; Write-Host "    block -> $a"
        Ensure-Import $Repo 'CLAUDE.md'
        Ensure-Import $Repo 'GEMINI.md'
    }
}

$Upstream = if (Test-Path (Join-Path $Here 'UPSTREAM.txt')) {
    (Get-Content (Join-Path $Here 'UPSTREAM.txt') -Raw).Trim()
} else { 'unknown' }
$Version = if (Test-Path (Join-Path $Here 'VERSION')) {
    (Get-Content (Join-Path $Here 'VERSION') -Raw).Trim()
} else { 'unknown' }

# The source may be a temporary download (boot.ps1): point at the copies when there are any.
$PromptNote = if ($PDests.Count) { 'Prompts installed to: ' + ($PDests -join ', ') } else { "Prompts to start from: $Here\prompts\ (re-run with -Prompts to copy them next to the skills)" }
Write-Host @"

Installed from: $Here, version $Version (see CHANGELOG.md)
Upstream: $Upstream
Next:
  - Claude Code: skills appear as /<name>; the oh- prefix keeps them clear of built-in skills such as /code-review.
  - OpenCode: reads .claude/skills, .agents/skills and .opencode/skills; ask for one by name (v2 also documents /<name>).
  - OpenHands: turn on load_project_skills (or load_user_skills for -User) in Settings -> Agent.
  - Codex: reads .agents\skills in the repo (and its parent folders) and ~\.agents\skills (-Tool codex, in the default set);
    restart Codex, then /skills lists them and `$<name> mentions one. Gemini CLI reads the same directory.
$PromptNote
"@
