# ai-skills one-line installer for Windows (no checkout needed).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.ps1 | iex"
#
# It downloads the repository archive of one ref into a temporary directory,
# runs install.ps1 from it and removes the download. Flags for install.ps1
# come from the AI_SKILLS_ARGS environment variable (default: -User -Skills all
# -Prompts -AgentsMd, i.e. every skill and the prompts for Claude Code and Codex
# user-wide, plus the review-gates block in the agent instructions), for example:
#
#   $env:AI_SKILLS_ARGS = '-User -Skills core'; irm .../boot.ps1 | iex
#
# Re-running upgrades in place: install.ps1 replaces the selected skills.
#
# Optional environment:
#   AI_SKILLS_REF=<branch|tag|commit>   what to install (default: main)
#   AI_SKILLS_REPO=<owner/name>         the repository (default: BlackVS/aiskills)
#   AI_SKILLS_BASE=<url>                download from this Gitea server (for example a
#                                       mirror) instead of GitHub
#   AI_SKILLS_TOKEN=<token>             sent as an Authorization header when set (GitHub:
#                                       downloads through the API, for a private fork)
#   AI_SKILLS_ARCHIVE=<path>            install from a local .tar.gz instead of downloading
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) { throw "ERROR: 'tar' is required (Windows 10 1803+ ships it) but not found." }
$ref = if ($env:AI_SKILLS_REF) { $env:AI_SKILLS_REF } else { 'main' }
$base = $env:AI_SKILLS_BASE
$repo = if ($env:AI_SKILLS_REPO) { $env:AI_SKILLS_REPO } else { 'BlackVS/aiskills' }
$headers = @{}
if ($base) {
    $url = "$base/api/v1/repos/$repo/archive/$ref.tar.gz"
    if ($env:AI_SKILLS_TOKEN) { $headers['Authorization'] = "token $env:AI_SKILLS_TOKEN" }
} elseif ($env:AI_SKILLS_TOKEN) {
    $url = "https://api.github.com/repos/$repo/tarball/$ref"
    $headers['Authorization'] = "Bearer $env:AI_SKILLS_TOKEN"
} else {
    $url = "https://github.com/$repo/archive/$ref.tar.gz"
}
$dest = Join-Path ([IO.Path]::GetTempPath()) ('ai-skills-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $dest | Out-Null
try {
    $tgz = Join-Path $dest 'src.tar.gz'
    if ($env:AI_SKILLS_ARCHIVE) {
        Write-Host "Unpacking ai-skills from $env:AI_SKILLS_ARCHIVE ..."
        Copy-Item $env:AI_SKILLS_ARCHIVE $tgz
    } else {
        $from = if ($base) { $base } else { 'https://github.com' }
        Write-Host "Fetching ai-skills $ref from $from/$repo ..."
        Invoke-WebRequest $url -OutFile $tgz -UseBasicParsing -Headers $headers
    }
    # extract by a relative name inside the temp dir: GNU tar (Git for Windows) reads a C:\ path as a remote host
    Push-Location $dest
    try { & tar -xzf 'src.tar.gz' --strip-components=1; if ($LASTEXITCODE -ne 0) { throw "ERROR: could not unpack the archive" } }
    finally { Pop-Location }
    Remove-Item $tgz
    $installer = Join-Path $dest 'install.ps1'
    if (-not (Test-Path $installer)) { throw "ERROR: the archive has no install.ps1 (wrong ref or repository?)" }
    $argString = if ($env:AI_SKILLS_ARGS) { $env:AI_SKILLS_ARGS } else {
        $v = if (Test-Path (Join-Path $dest 'VERSION')) { (Get-Content (Join-Path $dest 'VERSION') -Raw).Trim() } else { 'unknown' }
        Write-Host "No AI_SKILLS_ARGS given: running install.ps1 -User -Skills all -Prompts -AgentsMd (version $v)"
        '-User -Skills all -Prompts -AgentsMd'
    }
    # The flags are a plain string (from the environment), so hand them to the
    # installer through PowerShell's own parser, exactly as typed on a command line.
    Invoke-Expression ("& '" + $installer.Replace("'", "''") + "' " + $argString)
} finally {
    Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
}
