# aiskills one-line installer for Windows (no checkout needed).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BlackVS/aiskills/main/boot.ps1 | iex"
#
# It downloads one release's archive into a temporary directory, checks it
# against the release's SHA256SUMS, runs install.ps1 from it and removes the
# download. The archive's digest is recorded in the installed manifest
# (<dest>\.ai-skills.json, "archive_sha256"). Flags for install.ps1
# come from the AI_SKILLS_ARGS environment variable (default: -User -Skills all
# -Prompts -AgentsMd, i.e. every skill and the prompts for Claude Code and Codex
# user-wide, plus the review-gates block in the agent instructions), for example:
#
#   $env:AI_SKILLS_ARGS = '-User -Skills core'; irm .../boot.ps1 | iex
#
# Re-running upgrades in place: install.ps1 replaces the selected skills.
#
# A release (vX.Y.Z) is installed only from its release asset
# aiskills-X.Y.Z.tar.gz whose SHA-256 matches the release's SHA256SUMS: a
# missing SHA256SUMS or a mismatch stops before anything is installed. Any
# other ref (a branch such as main, a commit) has no such check and is refused
# unless AI_SKILLS_UNVERIFIED=1 is set; it is then installed from the source
# archive and reported as unverified.
#
# Optional environment:
#   AI_SKILLS_REF=<vX.Y.Z|branch|commit> what to install (default: the latest GitHub release;
#                                       main, unverified, with AI_SKILLS_BASE or while the
#                                       repository has no release)
#   AI_SKILLS_UNVERIFIED=1              allow a ref that is not a release, without verification
#   AI_SKILLS_REPO=<owner/name>         the repository (default: BlackVS/aiskills)
#   AI_SKILLS_BASE=<url>                download from this Gitea server (for example a
#                                       mirror) instead of GitHub
#   AI_SKILLS_TOKEN=<token>             sent as an Authorization header when set (GitHub:
#                                       downloads through the API, for a private fork)
#   AI_SKILLS_ARCHIVE=<path>            install from a local .tar.gz instead of downloading
#                                       (your own file: no release check)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) { throw "ERROR: 'tar' is required (Windows 10 1803+ ships it) but not found." }
$ref = $env:AI_SKILLS_REF
$base = $env:AI_SKILLS_BASE
$repo = if ($env:AI_SKILLS_REPO) { $env:AI_SKILLS_REPO } else { 'BlackVS/aiskills' }
if (-not $ref -and -not $env:AI_SKILLS_ARCHIVE) {
    if ($base) {
        $ref = 'main'
    } elseif ($env:AI_SKILLS_TOKEN) {
        # A private fork: the API with the token; 404 means no release. Only a
        # clear "no release" answer falls back to main; any other failure stops,
        # so a lookup error never installs unreleased work.
        try {
            $ref = (Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -UseBasicParsing -Headers @{ Authorization = "Bearer $env:AI_SKILLS_TOKEN"; Accept = 'application/vnd.github+json' }).tag_name
            if (-not $ref) { throw 'no tag_name in the API answer' }
        } catch {
            $status = 0
            if ($_.Exception.Response) { try { $status = [int]$_.Exception.Response.StatusCode } catch { } }
            if ($status -ne 404) { throw "ERROR: could not look up the latest release of $repo ($($_.Exception.Message)); set AI_SKILLS_REF to skip the lookup" }
            $ref = $null
        }
    } else {
        # The latest release, not the tip of main: main can carry unreleased work.
        # /releases/latest redirects to /releases/tag/<tag> (no API rate limit),
        # or to /releases while the repository has none; anything else stops.
        $req = [Net.HttpWebRequest]::Create("https://github.com/$repo/releases/latest")
        $req.Method = 'HEAD'; $req.AllowAutoRedirect = $false; $req.UserAgent = 'aiskills-boot'
        try { $resp = $req.GetResponse() }
        catch { throw "ERROR: could not look up the latest release of $repo ($($_.Exception.Message)); set AI_SKILLS_REF to skip the lookup" }
        try { $location = $resp.Headers['Location'] } finally { $resp.Close() }
        if ($location -match '/releases/tag/([^/?#]+)') { $ref = [Uri]::UnescapeDataString($Matches[1]) }
        elseif ($location -notmatch '/releases$') { throw "ERROR: could not look up the latest release of $repo (unexpected answer: $(if ($location) { $location } else { 'no redirect' })); set AI_SKILLS_REF to skip the lookup" }
    }
    if (-not $ref) {
        Write-Host "No release of $repo found: falling back to main."
        $ref = 'main'
    }
}
# Why a web request failed: not found (HTTP 404), another HTTP status from the server, or no answer at all.
function Get-FailureReason($ErrorRecord) {
    $status = 0
    if ($ErrorRecord.Exception.Response) { try { $status = [int]$ErrorRecord.Exception.Response.StatusCode } catch { } }
    if ($status -eq 404) { 'not found (HTTP 404)' }
    elseif ($status -gt 0) { "the server answered HTTP $status" }
    else { "no answer from the server (a network or transport error: $($ErrorRecord.Exception.Message))" }
}
function Get-DownloadFailure($ErrorRecord) {
    if ($ErrorRecord.Exception -is [IO.FileNotFoundException]) { $ErrorRecord.Exception.Message } else { Get-FailureReason $ErrorRecord }
}
# a release is exactly vMAJOR.MINOR.PATCH, as the release workflow tags it (the whole value)
$release = [bool]$ref -and $ref -cmatch '\Av[0-9]+\.[0-9]+\.[0-9]+\z'
$dest = Join-Path ([IO.Path]::GetTempPath()) ('aiskills-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $dest | Out-Null
# `irm | iex` runs in the caller's session: the digest handed to the installer is restored afterwards
$callerDigest = $env:AI_SKILLS_ARCHIVE_SHA256
try {
    $tgz = Join-Path $dest 'src.tar.gz'
    $from = if ($base) { $base } else { 'https://github.com' }
    if ($env:AI_SKILLS_ARCHIVE) {
        Write-Host "Unpacking aiskills from $env:AI_SKILLS_ARCHIVE ..."
        Copy-Item $env:AI_SKILLS_ARCHIVE $tgz
    } elseif ($release) {
        $name = "aiskills-$($ref.Substring(1)).tar.gz"
        $sums = Join-Path $dest 'SHA256SUMS'
        $assets = $null
        if (-not $base -and $env:AI_SKILLS_TOKEN) {
            # a private fork: assets are downloaded through the API, by the id the release lists for the name
            try { $assets = (Invoke-RestMethod "https://api.github.com/repos/$repo/releases/tags/$ref" -UseBasicParsing -Headers @{ Authorization = "Bearer $env:AI_SKILLS_TOKEN"; Accept = 'application/vnd.github+json' }).assets }
            catch { throw "ERROR: could not read release $ref of $repo`: $(Get-FailureReason $_); nothing was installed" }
        }
        function Get-Asset([string]$Name, [string]$OutFile) {
            if ($base) {
                $h = @{}; if ($env:AI_SKILLS_TOKEN) { $h['Authorization'] = "token $env:AI_SKILLS_TOKEN" }
                Invoke-WebRequest "$base/$repo/releases/download/$ref/$Name" -OutFile $OutFile -UseBasicParsing -Headers $h
            } elseif ($env:AI_SKILLS_TOKEN) {
                $a = @($assets | Where-Object { $_.name -ceq $Name })
                if ($a.Count -ne 1) { throw [IO.FileNotFoundException]::new('not found (the release lists no such asset)') }
                Invoke-WebRequest $a[0].url -OutFile $OutFile -UseBasicParsing -Headers @{ Authorization = "Bearer $env:AI_SKILLS_TOKEN"; Accept = 'application/octet-stream' }
            } else {
                Invoke-WebRequest "https://github.com/$repo/releases/download/$ref/$Name" -OutFile $OutFile -UseBasicParsing
            }
        }
        Write-Host "Fetching aiskills $ref ($name and SHA256SUMS) from $from/$repo ..."
        try { Get-Asset $name $tgz } catch { throw "ERROR: could not download $name of release $ref of $repo`: $(Get-DownloadFailure $_); nothing was installed" }
        try { Get-Asset 'SHA256SUMS' $sums } catch { throw "ERROR: could not download SHA256SUMS of release $ref of $repo`: $(Get-DownloadFailure $_); nothing was installed" }
        # the line for the name: "<digest>  NAME" (or " *NAME", binary mode); exactly one, and a digest
        $want = @(Get-Content -LiteralPath $sums | ForEach-Object {
            $f = -split $_
            if ($f.Count -eq 2 -and $f[1].TrimStart('*') -ceq $name) { $f[0].ToLowerInvariant() }
        })
        if ($want.Count -ne 1 -or $want[0] -cnotmatch '\A[0-9a-f]{64}\z') { throw "ERROR: SHA256SUMS of release $ref lists no single digest for $name; nothing was installed" }
        $got = (Get-FileHash -LiteralPath $tgz -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($got -cne $want[0]) { throw "ERROR: $name does not match SHA256SUMS of release $ref (expected $($want[0]), got $got); nothing was installed" }
        Write-Host "Verified $name against SHA256SUMS: $got"
    } else {
        if ($env:AI_SKILLS_UNVERIFIED -ne '1') { throw "ERROR: $ref is not a release (vX.Y.Z): only a release archive can be verified against its SHA256SUMS. Set AI_SKILLS_UNVERIFIED=1 to install $ref without verification." }
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
        Write-Host "WARNING: installing $ref UNVERIFIED (not a release; AI_SKILLS_UNVERIFIED=1) from $from/$repo ..."
        try { Invoke-WebRequest $url -OutFile $tgz -UseBasicParsing -Headers $headers }
        catch { throw "ERROR: could not download $ref of $repo`: $(Get-FailureReason $_); nothing was installed" }
    }
    # the digest of what is installed, for the manifest (install.ps1 validates it)
    $env:AI_SKILLS_ARCHIVE_SHA256 = (Get-FileHash -LiteralPath $tgz -Algorithm SHA256).Hash.ToLowerInvariant()
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
    $env:AI_SKILLS_ARCHIVE_SHA256 = $callerDigest
    Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
}
