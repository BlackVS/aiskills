"""The one-line installers unpack an archive and run the real installer from it,
offline here (AI_SKILLS_ARCHIVE points at an archive built from the checkout).

Run: python3 -m unittest tests/test_boot.py
"""
import os, pathlib, shutil, subprocess, sys, tempfile, unittest

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
# FAKE_API_STATUS (000: a transport failure); anything else streams FAKE_ARCHIVE.
echo "$*" >> "$FAKE_LOG"
out=; head=0; api=0
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; -fsSI) head=1 ;; -w) api=1; shift ;; esac
  shift
done
if [ $head = 1 ]; then
  printf 'HTTP/2 302\r\n'; [ -z "$FAKE_LOCATION" ] || printf 'location: %s\r\n' "$FAKE_LOCATION"; printf '\r\n'
elif [ $api = 1 ]; then
  printf '%s' "$FAKE_API_BODY" > "$out"; printf '%s' "$FAKE_API_STATUS"
  [ "$FAKE_API_STATUS" != 000 ] || exit 7
else
  cat "$FAKE_ARCHIVE"
fi
"""


@unittest.skipUnless(BASH and os.name != "nt", "needs a POSIX bash to put a fake curl on PATH")
class BashBootRelease(unittest.TestCase):
    """Without AI_SKILLS_REF the one-liner installs the latest release, main only when
    there is none, and stops when the lookup fails."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-boot-rel-"))
        self.home = self.tmp / "home"; self.home.mkdir()
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL); (self.bin / "curl").chmod(0o755)
        self.archive = build_archive(self.tmp); self.log = self.tmp / "curl.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_boot(self, location="", ok=True, **extra):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:{env['PATH']}", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_LOCATION=location, **extra)
        r = subprocess.run([BASH, str(ROOT / "boot.sh"), "--user", "-s", "core"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r, self.log.read_text().splitlines()

    def assert_stopped(self, r, calls):
        self.assertIn("could not look up the latest release", r.stderr)
        self.assertEqual(len(calls), 1, "no archive is downloaded after a failed lookup")
        self.assertFalse((self.home / ".claude").exists())

    def test_latest_release_is_installed(self):
        r, calls = self.run_boot("https://github.com/BlackVS/aiskills/releases/tag/v9.9.9")
        self.assertIn("https://github.com/BlackVS/aiskills/releases/latest", calls[0])
        self.assertIn("https://github.com/BlackVS/aiskills/archive/v9.9.9.tar.gz", calls[1])
        self.assertTrue((self.home / ".claude/skills/oh-code-review/SKILL.md").is_file())

    def test_no_release_falls_back_to_main(self):
        r, calls = self.run_boot("https://github.com/BlackVS/aiskills/releases")
        self.assertIn("No release of BlackVS/aiskills found: installing main.", r.stdout)
        self.assertIn("/archive/main.tar.gz", calls[1])

    def test_unexpected_redirect_stops(self):
        for location in ("", "https://github.com/login"):
            with self.subTest(location=location):
                self.log.unlink(missing_ok=True)
                self.assert_stopped(*self.run_boot(location, ok=False))

    def test_explicit_ref_skips_the_lookup(self):
        r, calls = self.run_boot("unused", AI_SKILLS_REF="v1.0.0")
        self.assertEqual(len(calls), 1)
        self.assertIn("/archive/v1.0.0.tar.gz", calls[0])

    def test_token_latest_release(self):
        r, calls = self.run_boot(AI_SKILLS_TOKEN="t", FAKE_API_STATUS="200", FAKE_API_BODY='{"tag_name":"v9.9.9"}')
        self.assertIn("https://api.github.com/repos/BlackVS/aiskills/releases/latest", calls[0])
        self.assertIn("https://api.github.com/repos/BlackVS/aiskills/tarball/v9.9.9", calls[1])

    def test_token_no_release_falls_back_to_main(self):
        r, calls = self.run_boot(AI_SKILLS_TOKEN="t", FAKE_API_STATUS="404", FAKE_API_BODY='{"message":"Not Found"}')
        self.assertIn("No release of BlackVS/aiskills found: installing main.", r.stdout)
        self.assertIn("/tarball/main", calls[1])

    def test_token_lookup_error_stops(self):
        for status in ("000", "401", "403", "500"):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                self.assert_stopped(*self.run_boot(ok=False, AI_SKILLS_TOKEN="t", FAKE_API_STATUS=status, FAKE_API_BODY="{}"))
        self.log.unlink(missing_ok=True)
        self.assert_stopped(*self.run_boot(ok=False, AI_SKILLS_TOKEN="t", FAKE_API_STATUS="200", FAKE_API_BODY="{}"))


# PowerShell 7 (pwsh) builds the HTTP error the way Invoke-RestMethod raises it there.
PWSH7 = shutil.which("pwsh")
FAKE_PS = r"""
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing, $Headers)
    Add-Content -LiteralPath $env:FAKE_LOG "download $Uri"; Copy-Item -LiteralPath $env:FAKE_ARCHIVE $OutFile }
function Invoke-RestMethod { param($Uri, [switch]$UseBasicParsing, $Headers)
    Add-Content -LiteralPath $env:FAKE_LOG "api $Uri"
    $status = [int]$env:FAKE_API_STATUS
    if ($status -eq 0) { throw [System.Net.Http.HttpRequestException]::new('connection refused') }
    if ($status -ne 200) { throw [Microsoft.PowerShell.Commands.HttpResponseException]::new("HTTP $status", [System.Net.Http.HttpResponseMessage]::new($status)) }
    $env:FAKE_API_BODY | ConvertFrom-Json }
"""


@unittest.skipUnless(PWSH7, "no pwsh found")
class PowerShellBootRelease(unittest.TestCase):
    """The authenticated lookup of boot.ps1: 404 means no release, any other failure stops."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-boot-rel-"))
        self.home = self.tmp / "home"; self.home.mkdir()
        self.archive = build_archive(self.tmp); self.log = self.tmp / "calls.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_boot(self, status, body="{}", ok=True):
        env = {k: v for k, v in clean_env().items() if not k.startswith("AI_SKILLS_")}
        env.update(AI_SKILLS_TOKEN="t", AI_SKILLS_ARGS="-User -Skills core", FAKE_LOG=str(self.log),
                   FAKE_ARCHIVE=str(self.archive), FAKE_API_STATUS=status, FAKE_API_BODY=body)
        script = f"Set-Variable -Name HOME -Value '{self.home}' -Force; " + FAKE_PS + "; & '" + str(ROOT / "boot.ps1") + "'"
        r = subprocess.run([PWSH7, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r, self.log.read_text().splitlines()

    def test_latest_release(self):
        r, calls = self.run_boot("200", '{"tag_name":"v9.9.9"}')
        self.assertEqual(calls, ["api https://api.github.com/repos/BlackVS/aiskills/releases/latest",
                                 "download https://api.github.com/repos/BlackVS/aiskills/tarball/v9.9.9"])

    def test_no_release_falls_back_to_main(self):
        r, calls = self.run_boot("404")
        self.assertIn("No release of BlackVS/aiskills found: installing main.", r.stdout)
        self.assertEqual(calls[1], "download https://api.github.com/repos/BlackVS/aiskills/tarball/main")

    def test_lookup_error_stops(self):
        for status, body in (("0", "{}"), ("401", "{}"), ("500", "{}"), ("200", "{}")):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                r, calls = self.run_boot(status, body, ok=False)
                self.assertIn("could not look up the latest release", r.stdout + r.stderr)
                self.assertEqual(len(calls), 1, "no archive is downloaded after a failed lookup")
                self.assertFalse((self.home / ".claude").exists())


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
