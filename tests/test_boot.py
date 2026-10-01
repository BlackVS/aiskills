"""The one-line installers unpack an archive and run the real installer from it,
offline here (AI_SKILLS_ARCHIVE points at an archive built from the checkout).

Run: python3 -m unittest tests/test_boot.py
"""
import hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest

from tests.test_install import BASH, PWSH, ROOT, clean_env, posix

CORE = ("architecture-review", "oh-code-review", "oh-technical-writing")


def build_archive(tmp):
    """A tar.gz of the working tree's tracked files with the top-level prefix the forge uses."""
    out = tmp / "aiskills.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", "--prefix=aiskills/", "-o", str(out), "HEAD"], cwd=ROOT, check=True)
    # the working tree may be ahead of HEAD while developing: overlay the current installers and skills
    stage = tmp / "stage"; shutil.rmtree(stage, ignore_errors=True); (stage / "aiskills").mkdir(parents=True)
    for name in ("install.sh", "install.ps1", "VERSION", "boot.sh", "boot.ps1"):
        shutil.copy(ROOT / name, stage / "aiskills" / name)
    shutil.copytree(ROOT / "skills", stage / "aiskills" / "skills")
    shutil.copytree(ROOT / "prompts", stage / "aiskills" / "prompts")
    shutil.copytree(ROOT / "agents", stage / "aiskills" / "agents")
    subprocess.run(["tar", "-czf", str(out), "-C", str(stage), "aiskills"], check=True)
    return out


class BootContract:
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-boot-"))
        self.home = self.tmp / "home"; self.home.mkdir()
        self.archive = build_archive(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archive_digest_reaches_the_manifest(self):
        r = self.run_boot()
        self.assert_user_install(r)
        m = json.loads((self.home / ".claude/skills/.ai-skills.json").read_text(encoding="utf-8"))
        self.assertEqual(m["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def assert_user_install(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for client_dir in (".claude/skills", ".agents/skills"):
            for s in CORE:
                self.assertTrue((self.home / client_dir / s / "SKILL.md").is_file(), f"~/{client_dir}/{s} missing")
        self.assertTrue((self.home / ".claude/prompts").is_dir(), "prompts copied by the default flags")
        self.assertIn("Prompts installed to:", r.stdout); self.assertNotIn("Prompts to start from", r.stdout)  # never the deleted download
        self.assertIn("aiskills:review-gates start", (self.home / ".claude/CLAUDE.md").read_text(encoding="utf-8"))


@unittest.skipUnless(BASH, "no bash found")
class BashBoot(BootContract, unittest.TestCase):
    def run_boot(self, *args, archive=None):
        env = dict(clean_env(), HOME=posix(self.home), AI_SKILLS_ARCHIVE=posix(archive or self.archive))
        return subprocess.run([BASH, posix(ROOT / "boot.sh"), *args], capture_output=True, text=True, env=env)

    def test_default_flags_install_user_wide(self):
        r = self.run_boot()
        self.assert_user_install(r)
        self.assertIn("No flags given: running install.sh --user -s all -p -a", r.stdout)

    def test_flags_pass_through(self):
        r = self.run_boot("--user", "-s", "oh-code-review", "-t", "codex")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home / ".agents/skills/oh-code-review/SKILL.md").is_file())
        self.assertFalse((self.home / ".claude").exists())

    def test_project_level_install(self):
        repo = self.tmp / "repo"; repo.mkdir()
        r = self.run_boot(posix(repo), "--agents-md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for client_dir in (".claude/skills", ".agents/skills"):
            self.assertTrue((repo / client_dir / "oh-code-review/SKILL.md").is_file(), f"{client_dir} missing")
        self.assertIn("aiskills:review-gates start", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".claude").exists(), "project level touches no user directory")

    def test_bad_archive_is_reported(self):
        bad = self.tmp / "bad.tar.gz"; bad.write_bytes(b"not an archive")
        r = self.run_boot(archive=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())


FAKE_CURL = r"""#!/bin/sh
# Records each call. A HEAD request (-fsSI) answers with FAKE_LOCATION (none when
# empty); an API request (-w) writes FAKE_API_BODY to its -o file and prints
# FAKE_API_STATUS (000: a transport failure). Any other request is a download,
# written to its -o file: SHA256SUMS (or API asset 2) is FAKE_SUMS (a 404 when
# unset), a release by tag is FAKE_RELEASE, anything else FAKE_ARCHIVE.
echo "$*" >> "$FAKE_LOG"
out=; head=0; api=0; url=
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; -fsSI) head=1 ;; -w) api=1; shift ;; -H) shift ;; -*) ;; *) url=$1 ;; esac
  shift
done
emit() { if [ -n "$out" ]; then cat > "$out"; else cat; fi; }
if [ $head = 1 ]; then
  printf 'HTTP/2 302\r\n'; [ -z "$FAKE_LOCATION" ] || printf 'location: %s\r\n' "$FAKE_LOCATION"; printf '\r\n'
elif [ $api = 1 ]; then
  printf '%s' "$FAKE_API_BODY" > "$out"; printf '%s' "$FAKE_API_STATUS"
  [ "$FAKE_API_STATUS" != 000 ] || exit 7
else
  case "$url" in
    */SHA256SUMS|*/releases/assets/2) [ -n "$FAKE_SUMS" ] || exit 22; emit < "$FAKE_SUMS" ;;
    */releases/tags/*) [ -n "$FAKE_RELEASE" ] || exit 22; printf '%s' "$FAKE_RELEASE" | emit ;;
    *) emit < "$FAKE_ARCHIVE" ;;
  esac
fi
"""
# A release as the API lists it: each asset's API url comes before its name.
RELEASE_JSON = ('{"url": "https://api.github.com/repos/BlackVS/aiskills/releases/7", "name": "aiskills 9.9.9", "assets": ['
                '{"url": "https://api.github.com/repos/BlackVS/aiskills/releases/assets/1", "id": 1, "name": "aiskills-9.9.9.tar.gz",'
                ' "uploader": {"url": "https://api.github.com/users/someone", "login": "someone"}},'
                ' {"url": "https://api.github.com/repos/BlackVS/aiskills/releases/assets/2", "id": 2, "name": "SHA256SUMS"}]}')


def flat(text):
    """Output as one line: PowerShell 7 colors an error and wraps it into "     | " continuation lines."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*(?:\|\s*)?", " ", text)


def sha256_of(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def write_sums(tmp, *lines):
    p = tmp / "SHA256SUMS"
    p.write_text("".join(l + "\n" for l in lines), encoding="utf-8", newline="\n")
    return p


class ReleaseFixture:
    """A scratch home, an archive standing in for the release asset aiskills-9.9.9.tar.gz, and its SHA256SUMS."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-boot-rel-"))
        self.home = self.tmp / "home"; self.home.mkdir()
        self.archive = build_archive(self.tmp); self.log = self.tmp / "calls.log"
        self.digest = sha256_of(self.archive)
        self.sums = write_sums(self.tmp, f"{'0' * 64}  aiskills-9.9.9.zip", f"{self.digest}  aiskills-9.9.9.tar.gz")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_installed(self, r, digest):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads((self.home / ".claude/skills/.ai-skills.json").read_text(encoding="utf-8"))
        self.assertEqual(m["archive_sha256"], digest, "the archive's digest reaches the manifest")

    def assert_refused(self, r, message):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(message, flat(r.stdout + r.stderr))
        self.assertFalse((self.home / ".claude").exists(), "nothing is installed")



class ReleaseCases(ReleaseFixture):
    """Cases both boots share; run_boot(**env) is each boot's own."""

    def test_release_is_verified_and_its_digest_recorded(self):
        r = self.run_boot(AI_SKILLS_REF="v9.9.9")
        self.assert_installed(r, self.digest)
        self.assertIn(f"Verified aiskills-9.9.9.tar.gz against SHA256SUMS: {self.digest}", r.stdout)
        urls = " ".join(self.calls())
        self.assertIn("https://github.com/BlackVS/aiskills/releases/download/v9.9.9/aiskills-9.9.9.tar.gz", urls)
        self.assertIn("https://github.com/BlackVS/aiskills/releases/download/v9.9.9/SHA256SUMS", urls)
        self.assertNotIn("/archive/", urls, "never the source archive for a release")

    def test_binary_mode_and_uppercase_sums_are_accepted(self):
        write_sums(self.tmp, f"{self.digest.upper()} *aiskills-9.9.9.tar.gz")
        self.assert_installed(self.run_boot(AI_SKILLS_REF="v9.9.9"), self.digest)

    def test_mismatch_installs_nothing(self):
        write_sums(self.tmp, f"{'f' * 64}  aiskills-9.9.9.tar.gz")
        self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9"), "does not match SHA256SUMS of release v9.9.9")

    def test_missing_sums_installs_nothing(self):
        self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9", FAKE_SUMS=""), "has no SHA256SUMS")

    def test_sums_without_a_single_entry_install_nothing(self):
        for lines in ((f"{self.digest}  aiskills-9.9.8.tar.gz",),
                      (f"{self.digest}  aiskills-9.9.9.tar.gz", f"{self.digest}  aiskills-9.9.9.tar.gz"),
                      (f"{self.digest}  aiskills-9.9.9.tar.gz", "not-a-digest  aiskills-9.9.9.tar.gz"),
                      ("not-a-digest  aiskills-9.9.9.tar.gz",)):
            with self.subTest(lines=lines):
                write_sums(self.tmp, *lines)
                self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9"), "lists no single digest for aiskills-9.9.9.tar.gz")

    def test_non_release_ref_is_refused_without_the_opt_in(self):
        for ref in ("main", "0123abc", "v9.9.9-rc1", "9.9.9"):
            with self.subTest(ref=ref):
                self.log.unlink(missing_ok=True)
                self.assert_refused(self.run_boot(AI_SKILLS_REF=ref), f"{ref} is not a release (vX.Y.Z)")
                self.assertEqual(self.calls(), [], "nothing is downloaded")

    def test_non_release_ref_with_the_opt_in_is_unverified(self):
        r = self.run_boot(AI_SKILLS_REF="main", AI_SKILLS_UNVERIFIED="1")
        self.assert_installed(r, self.digest)
        self.assertIn("WARNING: installing main UNVERIFIED", r.stdout)
        self.assertIn("https://github.com/BlackVS/aiskills/archive/main.tar.gz", " ".join(self.calls()))

    def test_gitea_base_downloads_release_assets(self):
        r = self.run_boot(AI_SKILLS_REF="v9.9.9", AI_SKILLS_BASE="https://git.example.invalid")
        self.assert_installed(r, self.digest)
        urls = " ".join(self.calls())
        self.assertIn("https://git.example.invalid/BlackVS/aiskills/releases/download/v9.9.9/aiskills-9.9.9.tar.gz", urls)
        self.assertIn("https://git.example.invalid/BlackVS/aiskills/releases/download/v9.9.9/SHA256SUMS", urls)


@unittest.skipUnless(BASH and os.name != "nt", "needs a POSIX bash to put a fake curl on PATH (Git Bash puts its own curl first)")
class BashBootRelease(ReleaseCases, unittest.TestCase):
    """boot.sh: the latest release by default, verified against SHA256SUMS; main only on request."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL); (self.bin / "curl").chmod(0o755)

    def run_boot(self, location="", **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:{env['PATH']}", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_SUMS=str(self.sums), FAKE_RELEASE=RELEASE_JSON,
                   FAKE_LOCATION=location)
        env.update(extra)
        return subprocess.run([BASH, str(ROOT / "boot.sh"), "--user", "-s", "core"], capture_output=True, text=True, env=env)

    def assert_stopped(self, r):
        self.assert_refused(r, "could not look up the latest release")
        self.assertEqual(len(self.calls()), 1, "no archive is downloaded after a failed lookup")

    def test_latest_release_is_installed(self):
        r = self.run_boot("https://github.com/BlackVS/aiskills/releases/tag/v9.9.9")
        self.assert_installed(r, self.digest)
        calls = self.calls()
        self.assertIn("https://github.com/BlackVS/aiskills/releases/latest", calls[0])
        self.assertIn("https://github.com/BlackVS/aiskills/releases/download/v9.9.9/aiskills-9.9.9.tar.gz", calls[1])
        self.assertIn("https://github.com/BlackVS/aiskills/releases/download/v9.9.9/SHA256SUMS", calls[2])

    def test_no_release_needs_the_opt_in(self):
        r = self.run_boot("https://github.com/BlackVS/aiskills/releases")
        self.assert_refused(r, "main is not a release (vX.Y.Z)")
        self.assertIn("No release of BlackVS/aiskills found: falling back to main.", r.stdout)
        self.log.unlink()
        r = self.run_boot("https://github.com/BlackVS/aiskills/releases", AI_SKILLS_UNVERIFIED="1")
        self.assert_installed(r, self.digest)
        self.assertIn("/archive/main.tar.gz", self.calls()[1])

    def test_unexpected_redirect_stops(self):
        for location in ("", "https://github.com/login"):
            with self.subTest(location=location):
                self.log.unlink(missing_ok=True)
                self.assert_stopped(self.run_boot(location))

    def test_explicit_ref_skips_the_lookup(self):
        self.run_boot("unused", AI_SKILLS_REF="v9.9.9")
        self.assertNotIn("releases/latest", " ".join(self.calls()))

    def test_token_latest_release_downloads_assets_through_the_api(self):
        r = self.run_boot(AI_SKILLS_TOKEN="t", FAKE_API_STATUS="200", FAKE_API_BODY='{"tag_name":"v9.9.9"}')
        self.assert_installed(r, self.digest)
        calls = self.calls()
        self.assertIn("https://api.github.com/repos/BlackVS/aiskills/releases/latest", calls[0])
        self.assertIn("https://api.github.com/repos/BlackVS/aiskills/releases/tags/v9.9.9", calls[1])
        for call, asset in ((calls[2], 1), (calls[3], 2)):
            self.assertIn(f"https://api.github.com/repos/BlackVS/aiskills/releases/assets/{asset}", call)
            self.assertIn("Accept: application/octet-stream", call)

    def test_token_release_without_the_asset_installs_nothing(self):
        r = self.run_boot(AI_SKILLS_REF="v9.9.9", AI_SKILLS_TOKEN="t", FAKE_RELEASE='{"assets": []}')
        self.assert_refused(r, "has no aiskills-9.9.9.tar.gz asset")

    def test_token_no_release_needs_the_opt_in(self):
        r = self.run_boot(AI_SKILLS_TOKEN="t", AI_SKILLS_UNVERIFIED="1", FAKE_API_STATUS="404", FAKE_API_BODY='{"message":"Not Found"}')
        self.assert_installed(r, self.digest)
        self.assertIn("/tarball/main", self.calls()[1])

    def test_token_lookup_error_stops(self):
        for status in ("000", "401", "403", "500"):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                self.assert_stopped(self.run_boot(AI_SKILLS_TOKEN="t", FAKE_API_STATUS=status, FAKE_API_BODY="{}"))
        self.log.unlink(missing_ok=True)
        self.assert_stopped(self.run_boot(AI_SKILLS_TOKEN="t", FAKE_API_STATUS="200", FAKE_API_BODY="{}"))


# The web cmdlets boot.ps1 uses, faked: each call is recorded, and downloads are routed
# like the fake curl's. PowerShell 7 (pwsh) builds the HTTP error the way Invoke-RestMethod raises it there.
FAKE_PS = r"""
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing, $Headers)
    $accept = if ($Headers -and $Headers['Accept']) { "Accept: $($Headers['Accept']) " } else { '' }
    Add-Content -LiteralPath $env:FAKE_LOG "download $accept$Uri"
    if ($Uri -like '*/SHA256SUMS' -or $Uri -like '*/releases/assets/2') {
        if (-not $env:FAKE_SUMS) { throw 'HTTP 404' }
        Copy-Item -LiteralPath $env:FAKE_SUMS $OutFile
    } else { Copy-Item -LiteralPath $env:FAKE_ARCHIVE $OutFile } }
function Invoke-RestMethod { param($Uri, [switch]$UseBasicParsing, $Headers)
    Add-Content -LiteralPath $env:FAKE_LOG "api $Uri"
    if ($Uri -like '*/releases/tags/*') { return ($env:FAKE_RELEASE | ConvertFrom-Json) }
    $status = [int]$env:FAKE_API_STATUS
    if ($status -eq 0) { throw [System.Net.Http.HttpRequestException]::new('connection refused') }
    if ($status -ne 200) { throw [Microsoft.PowerShell.Commands.HttpResponseException]::new("HTTP $status", [System.Net.Http.HttpResponseMessage]::new($status)) }
    $env:FAKE_API_BODY | ConvertFrom-Json }
"""


def ps_boot(home, env):
    script = f"Set-Variable -Name HOME -Value '{home}' -Force; " + FAKE_PS + "; & '" + str(ROOT / "boot.ps1") + "'"
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, env=env)


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellBootVerify(ReleaseCases, unittest.TestCase):
    """boot.ps1 with an explicit ref: the release check, under Windows PowerShell 5.1 or PowerShell 7."""

    def run_boot(self, **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(AI_SKILLS_ARGS="-User -Skills core", FAKE_LOG=str(self.log), FAKE_ARCHIVE=str(self.archive),
                   FAKE_SUMS=str(self.sums), FAKE_RELEASE=RELEASE_JSON, FAKE_API_STATUS="200", FAKE_API_BODY="{}")
        env.update(extra)
        return ps_boot(self.home, env)

    def calls(self):
        return [c.split(" ", 1)[1] for c in super().calls()]

    def test_token_release_downloads_assets_through_the_api(self):
        r = self.run_boot(AI_SKILLS_REF="v9.9.9", AI_SKILLS_TOKEN="t")
        self.assert_installed(r, self.digest)
        self.assertEqual(self.calls(), ["https://api.github.com/repos/BlackVS/aiskills/releases/tags/v9.9.9",
                                        "Accept: application/octet-stream https://api.github.com/repos/BlackVS/aiskills/releases/assets/1",
                                        "Accept: application/octet-stream https://api.github.com/repos/BlackVS/aiskills/releases/assets/2"])

    def test_token_release_without_the_asset_installs_nothing(self):
        r = self.run_boot(AI_SKILLS_REF="v9.9.9", AI_SKILLS_TOKEN="t", FAKE_RELEASE='{"assets": []}')
        self.assert_refused(r, "has no aiskills-9.9.9.tar.gz asset")

    def test_callers_digest_variable_is_restored(self):
        # `irm | iex` runs boot.ps1 in the caller's session
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(AI_SKILLS_ARGS="-User -Skills core", AI_SKILLS_REF="v9.9.9", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_SUMS=str(self.sums), AI_SKILLS_ARCHIVE_SHA256="from-the-caller")
        script = (f"Set-Variable -Name HOME -Value '{self.home}' -Force; " + FAKE_PS + "; & '" + str(ROOT / "boot.ps1") + "'; "
                  "'after: ' + $env:AI_SKILLS_ARCHIVE_SHA256")
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, env=env)
        self.assert_installed(r, self.digest)
        self.assertIn("after: from-the-caller", r.stdout)


PWSH7 = shutil.which("pwsh")


@unittest.skipUnless(PWSH7, "no pwsh found")
class PowerShellBootRelease(ReleaseFixture, unittest.TestCase):
    """The authenticated lookup of boot.ps1: 404 means no release, any other failure stops."""

    def run_boot(self, status, body="{}", **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(AI_SKILLS_TOKEN="t", AI_SKILLS_ARGS="-User -Skills core", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_SUMS=str(self.sums), FAKE_RELEASE=RELEASE_JSON,
                   FAKE_API_STATUS=status, FAKE_API_BODY=body)
        env.update(extra)
        script = f"Set-Variable -Name HOME -Value '{self.home}' -Force; " + FAKE_PS + "; & '" + str(ROOT / "boot.ps1") + "'"
        return subprocess.run([PWSH7, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                              capture_output=True, text=True, env=env)

    def test_latest_release(self):
        r = self.run_boot("200", '{"tag_name":"v9.9.9"}')
        self.assert_installed(r, self.digest)
        self.assertEqual(self.calls(), ["api https://api.github.com/repos/BlackVS/aiskills/releases/latest",
                                        "api https://api.github.com/repos/BlackVS/aiskills/releases/tags/v9.9.9",
                                        "download Accept: application/octet-stream https://api.github.com/repos/BlackVS/aiskills/releases/assets/1",
                                        "download Accept: application/octet-stream https://api.github.com/repos/BlackVS/aiskills/releases/assets/2"])

    def test_no_release_needs_the_opt_in(self):
        r = self.run_boot("404")
        self.assert_refused(r, "main is not a release (vX.Y.Z)")
        self.assertIn("No release of BlackVS/aiskills found: falling back to main.", r.stdout)
        self.log.unlink()
        r = self.run_boot("404", AI_SKILLS_UNVERIFIED="1")
        self.assert_installed(r, self.digest)
        self.assertEqual(self.calls()[1], "download https://api.github.com/repos/BlackVS/aiskills/tarball/main")

    def test_lookup_error_stops(self):
        for status, body in (("0", "{}"), ("401", "{}"), ("500", "{}"), ("200", "{}")):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                r = self.run_boot(status, body)
                self.assert_refused(r, "could not look up the latest release")
                self.assertEqual(len(self.calls()), 1, "no archive is downloaded after a failed lookup")


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellBoot(BootContract, unittest.TestCase):
    def run_boot(self, args=None, archive=None):
        script = f"Set-Variable -Name HOME -Value '{self.home}' -Force; $env:AI_SKILLS_ARCHIVE = '{archive or self.archive}'; "
        if args is not None:
            script += "$env:AI_SKILLS_ARGS = '" + args.replace("'", "''") + "'; "  # the flags string may itself quote a path
        script += "& '" + str(ROOT / "boot.ps1") + "'"
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, env=clean_env())

    def test_default_flags_install_user_wide(self):
        r = self.run_boot()
        self.assert_user_install(r)
        self.assertIn("No AI_SKILLS_ARGS given: running install.ps1 -User -Skills all -Prompts -AgentsMd", r.stdout)

    def test_flags_pass_through(self):
        r = self.run_boot(args="-User -Skills oh-code-review -Tool codex")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home / ".agents/skills/oh-code-review/SKILL.md").is_file())
        self.assertFalse((self.home / ".claude").exists())

    def test_project_level_install(self):
        repo = self.tmp / "my project"; repo.mkdir()  # a space in the path: the example quotes it
        r = self.run_boot(args=f"'{repo}' -AgentsMd")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for client_dir in (".claude/skills", ".agents/skills"):
            self.assertTrue((repo / client_dir / "oh-code-review/SKILL.md").is_file(), f"{client_dir} missing")
        self.assertIn("aiskills:review-gates start", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".claude").exists(), "project level touches no user directory")

    def test_bad_archive_is_reported(self):
        bad = self.tmp / "bad.tar.gz"; bad.write_bytes(b"not an archive")
        r = self.run_boot(archive=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
