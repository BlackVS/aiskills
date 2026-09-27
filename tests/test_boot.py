"""The one-line installers unpack an archive and run the real installer from it,
offline here (AI_SKILLS_ARCHIVE points at an archive built from the checkout).

Run: python3 -m unittest tests/test_boot.py
"""
import os, pathlib, shutil, subprocess, sys, tempfile, unittest

from tests.test_install import BASH, PWSH, ROOT, posix

CORE = ("architecture-review", "oh-code-review", "oh-technical-writing")


def build_archive(tmp):
    """A tar.gz of the working tree's tracked files with the top-level prefix the forge uses."""
    out = tmp / "ai-skills.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", "--prefix=ai-skills/", "-o", str(out), "HEAD"], cwd=ROOT, check=True)
    # the working tree may be ahead of HEAD while developing: overlay the current installers and skills
    stage = tmp / "stage"; shutil.rmtree(stage, ignore_errors=True); (stage / "ai-skills").mkdir(parents=True)
    for name in ("install.sh", "install.ps1", "VERSION", "boot.sh", "boot.ps1"):
        shutil.copy(ROOT / name, stage / "ai-skills" / name)
    shutil.copytree(ROOT / "skills", stage / "ai-skills" / "skills")
    shutil.copytree(ROOT / "prompts", stage / "ai-skills" / "prompts")
    shutil.copytree(ROOT / "agents", stage / "ai-skills" / "agents")
    subprocess.run(["tar", "-czf", str(out), "-C", str(stage), "ai-skills"], check=True)
    return out


class BootContract:
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-skills-boot-"))
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
        self.assertIn("ai-skills:review-gates start", (self.home / ".claude/CLAUDE.md").read_text(encoding="utf-8"))


@unittest.skipUnless(BASH, "no bash found")
class BashBoot(BootContract, unittest.TestCase):
    def run_boot(self, *args, archive=None):
        env = dict(os.environ, HOME=posix(self.home), AI_SKILLS_ARCHIVE=posix(archive or self.archive))
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
        self.assertIn("ai-skills:review-gates start", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".claude").exists(), "project level touches no user directory")

    def test_bad_archive_is_reported(self):
        bad = self.tmp / "bad.tar.gz"; bad.write_bytes(b"not an archive")
        r = self.run_boot(archive=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellBoot(BootContract, unittest.TestCase):
    def run_boot(self, args=None, archive=None):
        script = f"Set-Variable -Name HOME -Value '{self.home}' -Force; $env:AI_SKILLS_ARCHIVE = '{archive or self.archive}'; "
        if args is not None:
            script += "$env:AI_SKILLS_ARGS = '" + args.replace("'", "''") + "'; "  # the flags string may itself quote a path
        script += "& '" + str(ROOT / "boot.ps1") + "'"
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True)

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
        self.assertIn("ai-skills:review-gates start", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".claude").exists(), "project level touches no user directory")

    def test_bad_archive_is_reported(self):
        bad = self.tmp / "bad.tar.gz"; bad.write_bytes(b"not an archive")
        r = self.run_boot(archive=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
