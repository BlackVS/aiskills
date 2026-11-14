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
# empty); the API's latest-release lookup writes FAKE_API_BODY to its -o file and
# prints FAKE_API_STATUS (000: a transport failure). Anything else is a download,
# written to its -o file, its status printed for -w: SHA256SUMS (or API asset 2)
# is FAKE_SUMS (404 when unset), an export's MANIFEST FAKE_MANIFEST (404 when unset),
# a release by tag FAKE_RELEASE (404 when unset), anything else FAKE_ARCHIVE, or
# FAKE_ARCHIVE_STATUS when set (000: no answer).
echo "$*" >> "$FAKE_LOG"
out=; head=0; status=0; url=
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; -fsSI) head=1 ;; -w) status=1; shift ;; -H) shift ;; -*) ;; *) url=$1 ;; esac
  shift
done
emit() { if [ -n "$out" ]; then cat > "$out"; else cat; fi; }
answer() {  # answer CODE: the status line -w prints; 000 is a transport failure
  [ $status = 1 ] && printf '%s' "$1"
  [ "$1" != 000 ] || exit 7
  case "$1" in 2??) ;; *) [ $status = 1 ] || exit 22 ;; esac
}
if [ $head = 1 ]; then
  printf 'HTTP/2 302\r\n'; [ -z "$FAKE_LOCATION" ] || printf 'location: %s\r\n' "$FAKE_LOCATION"; printf '\r\n'
  exit 0
fi
case "$url" in
  */releases/latest)
    printf '%s' "$FAKE_API_BODY" > "$out"; printf '%s' "$FAKE_API_STATUS"
    [ "$FAKE_API_STATUS" != 000 ] || exit 7 ;;
  */SHA256SUMS|*/releases/assets/2)
    if [ -n "$FAKE_SUMS" ]; then emit < "$FAKE_SUMS"; answer 200; else echo 'Not Found' | emit; answer 404; fi ;;
  */MANIFEST)
    if [ -n "$FAKE_MANIFEST" ]; then emit < "$FAKE_MANIFEST"; answer 200; else echo 'Not Found' | emit; answer 404; fi ;;
  */releases/tags/*)
    if [ -n "$FAKE_RELEASE" ]; then printf '%s' "$FAKE_RELEASE" | emit; answer 200; else echo 'Not Found' | emit; answer 404; fi ;;
  *)
    if [ -n "$FAKE_ARCHIVE_STATUS" ]; then echo 'error page' | emit; answer "$FAKE_ARCHIVE_STATUS"
    else emit < "$FAKE_ARCHIVE"; answer 200; fi ;;
esac
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

    def test_release_without_boot_script_assets_installs(self):
        # Every release before the boot scripts became assets lists only the archives in SHA256SUMS
        # (self.sums); the connected path needs nothing else, so such a release installs as before.
        self.assertNotIn("boot.", self.sums.read_text())
        r = self.run_boot(AI_SKILLS_REF="v9.9.9")
        self.assert_installed(r, self.digest)
        self.assertNotIn("boot.", " ".join(self.calls()), "no boot script is fetched")

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
        self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9", FAKE_SUMS=""),
                            "could not download SHA256SUMS of release v9.9.9 of BlackVS/aiskills: not found (HTTP 404)")

    def test_sums_without_a_single_entry_install_nothing(self):
        for lines in ((f"{self.digest}  aiskills-9.9.8.tar.gz",),
                      (f"{self.digest}  aiskills-9.9.9.tar.gz", f"{self.digest}  aiskills-9.9.9.tar.gz"),
                      (f"{self.digest}  aiskills-9.9.9.tar.gz", "not-a-digest  aiskills-9.9.9.tar.gz"),
                      ("not-a-digest  aiskills-9.9.9.tar.gz",)):
            with self.subTest(lines=lines):
                write_sums(self.tmp, *lines)
                self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9"), "lists no single digest for aiskills-9.9.9.tar.gz")

    def test_a_failed_download_names_its_cause(self):
        # not found, a server error and no answer are told apart; nothing is installed either way
        for status, cause in (("404", "not found (HTTP 404)"), ("504", "the server answered HTTP 504"),
                              ("000", "no answer from the server")):
            with self.subTest(status=status):
                self.assert_refused(self.run_boot(AI_SKILLS_REF="v9.9.9", FAKE_ARCHIVE_STATUS=status),
                                    f"could not download aiskills-9.9.9.tar.gz of release v9.9.9 of BlackVS/aiskills: {cause}")
                self.assert_refused(self.run_boot(AI_SKILLS_REF="main", AI_SKILLS_UNVERIFIED="1", FAKE_ARCHIVE_STATUS=status),
                                    f"could not download main of BlackVS/aiskills: {cause}")

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
        self.assert_refused(r, "could not download aiskills-9.9.9.tar.gz of release v9.9.9 of BlackVS/aiskills: "
                               "not found (the release lists no such asset)")

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
class FakeHttpError : System.Exception {
    [object]$Response
    FakeHttpError([int]$status) : base("HTTP $status") { $this.Response = [pscustomobject]@{ StatusCode = $status } }
}
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing, $Headers)
    $accept = if ($Headers -and $Headers['Accept']) { "Accept: $($Headers['Accept']) " } else { '' }
    Add-Content -LiteralPath $env:FAKE_LOG "download $accept$Uri"
    if ($Uri -like '*/SHA256SUMS' -or $Uri -like '*/releases/assets/2') {
        if (-not $env:FAKE_SUMS) { throw [FakeHttpError]::new(404) }
        Copy-Item -LiteralPath $env:FAKE_SUMS $OutFile
    } elseif ($Uri -like '*/MANIFEST') {
        if (-not $env:FAKE_MANIFEST) { throw [FakeHttpError]::new(404) }
        Copy-Item -LiteralPath $env:FAKE_MANIFEST $OutFile
    } elseif ($env:FAKE_ARCHIVE_STATUS -eq '000') { throw [System.Net.WebException]::new('connection refused')
    } elseif ($env:FAKE_ARCHIVE_STATUS) { throw [FakeHttpError]::new([int]$env:FAKE_ARCHIVE_STATUS)
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
        self.assert_refused(r, "could not download aiskills-9.9.9.tar.gz of release v9.9.9 of BlackVS/aiskills: "
                               "not found (the release lists no such asset)")

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


class ExportCases(ReleaseFixture):
    """An aiforge export (format 1): MANIFEST and aiskills/<tag>/ with the release's assets and SHA256SUMS.
    run_boot(**env) and path(p) (the form the shell takes) are each boot's own."""

    MANIFEST = "aiforge-export 1\naimem v0.10.23\naiskills v9.9.9\naicrew v0.9.0\n"

    def setUp(self):
        super().setUp()
        self.export = self.tmp / "export"
        self.src = self.export / "aiskills" / "v9.9.9"; self.src.mkdir(parents=True)
        shutil.copy(self.archive, self.src / "aiskills-9.9.9.tar.gz")
        for name in ("boot.sh", "boot.ps1"):
            shutil.copy(ROOT / name, self.src / name)
        self.write_manifest(self.MANIFEST)
        self.write_export_sums(*(f"{sha256_of(f)}  {f.name}" for f in sorted(self.src.iterdir())))

    def write_manifest(self, text):
        (self.export / "MANIFEST").write_bytes(text.encode())

    def write_export_sums(self, *lines):
        (self.src / "SHA256SUMS").write_text("".join(l + "\n" for l in lines), encoding="utf-8", newline="\n")

    def run_export(self, export=None, **extra):
        return self.run_boot(AI_SKILLS_EXPORT=self.path(self.export) if export is None else export, **extra)

    def assert_from_export(self, r):
        self.assert_installed(r, self.digest)
        self.assertIn("Fetching aiskills v9.9.9 (aiskills-9.9.9.tar.gz and SHA256SUMS) from ", r.stdout)
        self.assertIn(f"Verified aiskills-9.9.9.tar.gz against SHA256SUMS: {self.digest}", r.stdout)
        self.assertNotIn("github.com", r.stdout + r.stderr)
        self.assertEqual(self.calls(), [], "a local export contacts no host")

    def assert_refused_quietly(self, r, message):
        # Windows PowerShell wraps a long error at the console width, inside a word: compare without whitespace
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(re.sub(r"\s", "", message), re.sub(r"\s", "", flat(r.stdout + r.stderr)))
        self.assertFalse((self.home / ".claude").exists(), "nothing is installed")
        self.assertNotIn("github.com", r.stdout + r.stderr)
        self.assertEqual(self.calls(), [], "nothing is downloaded")

    def test_installs_the_release_manifest_names(self):
        self.assert_from_export(self.run_export())

    def test_a_trailing_separator_and_crlf_manifest_are_accepted(self):
        self.write_manifest(self.MANIFEST.replace("\n", "\r\n"))
        self.assert_from_export(self.run_export(self.path(self.export) + "/"))

    def test_a_filesystem_root_export_stays_an_export(self):
        # the root normalizes to an empty prefix; the install must still read only the export, and fail there
        root = "/" if os.name != "nt" else os.path.splitdrive(str(self.tmp))[0] + "\\"
        prefix = root.rstrip("/\\")
        self.assert_refused_quietly(self.run_export(root), f"could not read {prefix}/MANIFEST")
        self.assert_refused_quietly(self.run_export(root, AI_SKILLS_REF="v9.9.9"),
                                    f"could not read {prefix}/aiskills/v9.9.9/aiskills-9.9.9.tar.gz")

    def test_an_explicit_release_needs_no_manifest(self):
        (self.export / "MANIFEST").unlink()
        self.assert_from_export(self.run_export(AI_SKILLS_REF="v9.9.9"))

    def test_manifest_refusals(self):
        x = self.path(self.export)
        for text, message in ((None, f"could not read {x}/MANIFEST"),
                              ("aiforge-export 2\naiskills v9.9.9\n", f"{x}/MANIFEST is 'aiforge-export 2', not a format this installer reads"),
                              ("aiforge-export 1\naimem v0.10.23\n", f"{x}/MANIFEST names no aiskills release"),
                              ("aiforge-export 1\naiskills main\n", "'main' is not a release (vX.Y.Z): an export holds releases only"),
                              ("aiforge-export 1\naiskills v9.9.9/../../x\n", "is not a release (vX.Y.Z)")):
            with self.subTest(text=text):
                (self.export / "MANIFEST").unlink(missing_ok=True)
                if text is not None:
                    self.write_manifest(text)
                self.assert_refused_quietly(self.run_export(), message)

    def test_a_missing_file_or_a_mismatch_installs_nothing(self):
        src = f"{self.path(self.export)}/aiskills/v9.9.9"
        (self.src / "aiskills-9.9.9.tar.gz").rename(self.tmp / "away.tar.gz")
        self.assert_refused_quietly(self.run_export(), f"could not read {src}/aiskills-9.9.9.tar.gz")
        (self.tmp / "away.tar.gz").rename(self.src / "aiskills-9.9.9.tar.gz")
        (self.src / "SHA256SUMS").rename(self.tmp / "SHA256SUMS.away")
        self.assert_refused_quietly(self.run_export(), f"could not read {src}/SHA256SUMS")
        self.write_export_sums(f"{'f' * 64}  aiskills-9.9.9.tar.gz")
        self.assert_refused_quietly(self.run_export(),
                                    f"{src}/aiskills-9.9.9.tar.gz does not match {src}/SHA256SUMS (expected {'f' * 64}")
        self.write_export_sums(f"{self.digest}  aiskills-9.9.8.tar.gz")
        self.assert_refused_quietly(self.run_export(), f"{src}/SHA256SUMS lists no single digest for aiskills-9.9.9.tar.gz")

    def test_refused_forms(self):
        for extra, message in (({"AI_SKILLS_BASE": "https://git.example.invalid"}, "AI_SKILLS_EXPORT (an aiforge export) and AI_SKILLS_BASE"),
                               ({"AI_SKILLS_ARCHIVE": str(self.archive)}, "AI_SKILLS_EXPORT and AI_SKILLS_ARCHIVE are both set"),
                               ({"AI_SKILLS_EXPORT": "http://files.example.invalid/x"}, "plain http is refused"),
                               ({"AI_SKILLS_EXPORT": "export"}, "AI_SKILLS_EXPORT must be an absolute directory")):
            with self.subTest(extra=extra):
                rest = {k: v for k, v in extra.items() if k != "AI_SKILLS_EXPORT"}
                self.assert_refused_quietly(self.run_export(extra.get("AI_SKILLS_EXPORT"), **rest), message)

    def test_https_export_reads_only_the_export(self):
        r = self.run_export("https://files.example.invalid/aiforge/", FAKE_MANIFEST=str(self.export / "MANIFEST"),
                            FAKE_ARCHIVE=str(self.src / "aiskills-9.9.9.tar.gz"), FAKE_SUMS=str(self.src / "SHA256SUMS"))
        self.assert_installed(r, self.digest)
        self.assertNotIn("github.com", r.stdout + r.stderr)
        urls = [u for c in self.calls() for u in c.split() if "://" in u]
        self.assertEqual(urls, ["https://files.example.invalid/aiforge/MANIFEST",
                                "https://files.example.invalid/aiforge/aiskills/v9.9.9/aiskills-9.9.9.tar.gz",
                                "https://files.example.invalid/aiforge/aiskills/v9.9.9/SHA256SUMS"])

    def test_the_mirror_form_is_unchanged(self):
        # AI_SKILLS_BASE alone still means a Gitea mirror
        r = self.run_boot(AI_SKILLS_REF="v9.9.9", AI_SKILLS_BASE="https://git.example.invalid")
        self.assert_installed(r, self.digest)
        self.assertIn("https://git.example.invalid/BlackVS/aiskills/releases/download/v9.9.9/SHA256SUMS", " ".join(self.calls()))


@unittest.skipUnless(BASH and os.name != "nt", "needs a POSIX bash to put a fake curl on PATH (Git Bash puts its own curl first)")
class BashBootExport(ExportCases, unittest.TestCase):
    """boot.sh with AI_SKILLS_EXPORT; the fake curl records any download."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL); (self.bin / "curl").chmod(0o755)

    def path(self, p):
        return str(p)

    def run_boot(self, **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:{env['PATH']}", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_SUMS=str(self.sums), FAKE_RELEASE=RELEASE_JSON, FAKE_LOCATION="")
        env.update(extra)
        return subprocess.run([BASH, str(ROOT / "boot.sh"), "--user", "-s", "core"], capture_output=True, text=True, env=env)

    def test_curl_is_held_to_https_redirects_included(self):
        # curl is told to stay on https, redirects included
        self.run_export("https://files.example.invalid/aiforge", FAKE_MANIFEST=str(self.export / "MANIFEST"))
        calls = self.calls()
        self.assertTrue(calls, "curl was called")
        for call in calls:
            self.assertIn("--proto =https --proto-redir =https", call)


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellBootExport(ExportCases, unittest.TestCase):
    """boot.ps1 with AI_SKILLS_EXPORT: a drive path on Windows, an absolute directory elsewhere."""

    def path(self, p):
        return str(p)

    def run_boot(self, **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(AI_SKILLS_ARGS="-User -Skills core", FAKE_LOG=str(self.log), FAKE_ARCHIVE=str(self.archive),
                   FAKE_SUMS=str(self.sums), FAKE_RELEASE=RELEASE_JSON, FAKE_API_STATUS="200", FAKE_API_BODY="{}")
        env.update(extra)
        return ps_boot(self.home, env)

    def calls(self):
        return [c.split(" ", 1)[1] for c in super().calls()]

    @unittest.skipUnless(os.name == "nt", "a \\\\host\\share path is a Windows form")
    def test_unc_path(self):
        drive, rest = os.path.splitdrive(str(self.export))
        unc = "\\\\localhost\\" + drive[0] + "$" + rest
        if not os.path.isfile(os.path.join(unc, "MANIFEST")):
            self.skipTest(f"the administrative share {unc} is not reachable here")
        self.assert_from_export(self.run_export(unc))


if __name__ == "__main__":
    unittest.main()
