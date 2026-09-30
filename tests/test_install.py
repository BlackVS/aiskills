"""Installer tests: both installers put complete skill directories where each
client reads them, on a scratch repo and a scratch home.

Run: python3 -m unittest tests/test_install.py
bash cases run wherever a bash is found (Linux, macOS, Git Bash on Windows);
PowerShell cases run where powershell/pwsh is found (Windows, or pwsh elsewhere).
"""
import os, pathlib, re, shutil, subprocess, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The managed block's markers, exactly as both installers write them.
BLOCK_MARKERS = ("<!-- ai-skills:review-gates start (managed by install.sh, do not edit inside) -->",
                 "<!-- ai-skills:review-gates end -->")
CORE = ("architecture-review", "oh-code-review", "oh-technical-writing")
BASH = shutil.which("bash") or next((p for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe") if os.path.exists(p)), None)
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def posix(p):
    return str(p).replace("\\", "/")


def clean_env(xdg_config=None):
    # XDG_CONFIG_HOME moves OpenCode's config dir; CI runners set it, so a test sets it only on purpose
    env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
    if xdg_config is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config)
    return env


def bash_install(*args, home=None, xdg_config=None):
    env = clean_env(xdg_config)
    if home is not None:
        env["HOME"] = posix(home)
    return subprocess.run([BASH, posix(ROOT / "install.sh"), *args], capture_output=True, text=True, env=env)


def ps_install(*args, home=None, xdg_config=None):
    script = ""
    if home is not None:
        script += f"Set-Variable -Name HOME -Value '{home}' -Force; "
    script += "& '" + str(ROOT / "install.ps1") + "' " + " ".join(a if re.fullmatch(r"-[A-Za-z]+", a) else "'" + a.replace("'", "''") + "'" for a in args)  # values quoted (empty, comma, space); parameter names bare
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, env=clean_env(xdg_config))


def complete(skill_dir):
    """A skill directory is complete when SKILL.md and every supporting file of the source are there."""
    src = ROOT / "skills" / skill_dir.name
    expected = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    got = sorted(p.relative_to(skill_dir) for p in skill_dir.rglob("*") if p.is_file())
    return expected == got


class InstallerContract:
    """Shared cases; subclasses bind `install` to one installer."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-skills-test-"))
        self.repo = self.tmp / "repo"; self.repo.mkdir()
        self.home = self.tmp / "home"; self.home.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_installs_for_claude_code_and_codex(self):
        r = self.install(str(self.repo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for client_dir in (".claude/skills", ".agents/skills"):
            for s in CORE:
                d = self.repo / client_dir / s
                self.assertTrue((d / "SKILL.md").is_file(), f"{client_dir}/{s} missing")
                self.assertTrue(complete(d), f"{client_dir}/{s} incomplete (references/scripts not copied)")
        self.assertTrue((self.repo / ".agents/skills/oh-code-review/references/dispositions.md").is_file())
        self.assertFalse((self.repo / ".opencode").exists())

    def test_codex_and_agents_name_the_same_directory(self):
        r = self.install(str(self.repo), "-t", "codex,agents")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo / ".agents/skills/oh-code-review/SKILL.md").is_file())
        self.assertFalse((self.repo / ".claude").exists())
        self.assertEqual(r.stdout.count("oh-code-review"), 1, "copied once, not once per name")

    def test_rerun_replaces_selected_skills_and_leaves_others(self):
        self.install(str(self.repo), "-s", "oh-code-review,oh-technical-writing")
        f = self.repo / ".agents/skills/oh-code-review/SKILL.md"
        f.write_text("edited\n", encoding="utf-8")
        other = self.repo / ".agents/skills/custom-skill"; other.mkdir(); (other / "SKILL.md").write_text("mine\n")
        r = self.install(str(self.repo), "-s", "oh-code-review")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(f.read_text(encoding="utf-8"), "edited\n", "an upgrade restores the shipped file")
        self.assertIn("replaced oh-code-review", r.stdout)
        self.assertEqual((other / "SKILL.md").read_text(), "mine\n", "unrelated skills are left alone")
        self.assertTrue((self.repo / ".agents/skills/oh-technical-writing/SKILL.md").is_file(), "unselected shipped skills are left alone")

    def test_user_level_destinations(self):
        r = self.install("--user" if self.flavor == "bash" else "-User", home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for client_dir in (".claude/skills", ".agents/skills"):
            self.assertTrue((self.home / client_dir / "oh-code-review/SKILL.md").is_file(), f"~/{client_dir} missing")

    def flags(self, *names):
        table = {"user": ("--user", "-User"), "prompts": ("-p", "-Prompts"), "agents": ("-a", "-AgentsMd")}
        return [table[n][0 if self.flavor == "bash" else 1] for n in names]

    def test_user_level_reaches_opencode_only_when_it_is_installed(self):
        # OpenCode v2 reads only AGENTS.md (no CLAUDE.md fallback) and runs commands from its config dir:
        # both are written when ~/.config/opencode exists, and nothing is created there otherwise
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.home / ".config").exists(), "no OpenCode here: nothing created under ~/.config")
        self.assertIn("ai-skills:review-gates start", (self.home / ".claude/CLAUDE.md").read_text(encoding="utf-8"))
        (self.home / ".config/opencode").mkdir(parents=True)
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        agents = self.home / ".config/opencode/AGENTS.md"
        self.assertTrue(agents.is_file(), "global AGENTS.md created for OpenCode")
        self.assertEqual(agents.read_text(encoding="utf-8").count("ai-skills:review-gates start"), 1)
        self.assertTrue((self.home / ".config/opencode/commands/code-review.md").is_file(), "prompts installed as OpenCode commands")
        self.assertFalse((self.home / ".config/opencode/prompts").exists())
        # A re-run replaces the managed block with the shipped one and keeps what is outside it.
        start, end = BLOCK_MARKERS
        agents.write_text(f"my notes before\n\n{start}\nstale gates text\n{end}\n\nmy notes after\n", encoding="utf-8")
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = agents.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(text.count("ai-skills:review-gates start"), 1, "replaced, never duplicated")
        self.assertNotIn("stale gates text", text)
        self.assertIn("my notes before", text)
        self.assertIn("my notes after", text)
        block = text.split(start + "\n", 1)[1].split(end, 1)[0]
        shipped = (ROOT / "agents/review-gates.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(block.rstrip("\n"), shipped.rstrip("\n"), "the block carries the shipped review-gates.md")

    def test_user_level_reaches_codex_and_gemini_only_when_they_are_installed(self):
        # Codex reads ~/.codex/AGENTS.md and Gemini CLI ~/.gemini/GEMINI.md: each is written (created if
        # absent) when the client's directory exists, and nothing is created for a client that is not there
        r = self.install(*self.flags("user", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.home / ".codex").exists()); self.assertFalse((self.home / ".gemini").exists())
        (self.home / ".codex").mkdir(); (self.home / ".gemini").mkdir()
        (self.home / ".gemini/GEMINI.md").write_text("# my gemini notes\n", encoding="utf-8")
        for _ in range(2):  # a re-run replaces the block, never duplicates it
            r = self.install(*self.flags("user", "agents"), home=self.home)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        codex = (self.home / ".codex/AGENTS.md").read_text(encoding="utf-8")
        gemini = (self.home / ".gemini/GEMINI.md").read_text(encoding="utf-8")
        self.assertEqual(codex.count("ai-skills:review-gates start"), 1, "created for Codex")
        self.assertEqual(gemini.count("ai-skills:review-gates start"), 1)
        self.assertTrue(gemini.startswith("# my gemini notes\n"), "the user's own notes are kept")

    def test_project_level_imports_agents_md_for_claude_and_gemini(self):
        # Claude Code reads CLAUDE.md and Gemini CLI GEMINI.md; both resolve "@AGENTS.md" imports
        (self.repo / "GEMINI.md").write_text("# project notes\n", encoding="utf-8")
        for _ in range(2):
            r = self.install(str(self.repo), *self.flags("agents"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.repo / "AGENTS.md").read_text(encoding="utf-8").count("ai-skills:review-gates start"), 1)
        self.assertEqual((self.repo / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n", "created with just the import")
        self.assertEqual((self.repo / "GEMINI.md").read_text(encoding="utf-8"), "# project notes\n\n@AGENTS.md\n", "import added once, notes kept")

    def test_user_level_opencode_follows_xdg_config_home(self):
        # OpenCode reads $XDG_CONFIG_HOME/opencode when it is set (observed with 1.18.32 and v2.0.18)
        xdg = self.tmp / "xdg"; (xdg / "opencode").mkdir(parents=True)
        tool = ("-t", "opencode") if self.flavor == "bash" else ("-Tool", "opencode")
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home, xdg_config=xdg)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.install(*self.flags("user"), *tool, home=self.home, xdg_config=xdg)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((xdg / "opencode/commands/code-review.md").is_file(), "prompts as commands")
        self.assertIn("ai-skills:review-gates start", (xdg / "opencode/AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((xdg / "opencode/skills/oh-code-review/SKILL.md").is_file(), "-t opencode user skills")
        self.assertFalse((self.home / ".config").exists(), "nothing written to ~/.config")

    def test_opencode_tool_installs_commands_not_prompts(self):
        tool = ("-t", "opencode") if self.flavor == "bash" else ("-Tool", "opencode")
        r = self.install(*tool, *self.flags("prompts"), str(self.repo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo / ".opencode/skills/oh-code-review/SKILL.md").is_file())
        self.assertTrue((self.repo / ".opencode/commands/code-review.md").is_file())
        self.assertFalse((self.repo / ".opencode/prompts").exists())
        self.assertFalse((self.repo / ".claude").exists())

    def test_empty_tool_selection_is_refused(self):
        for empty in ("", ",", ",,"):
            with self.subTest(value=empty):
                r = self.install(str(self.repo), "-t", empty)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("no tool selected", r.stdout + r.stderr)
                self.assertFalse((self.repo / ".claude").exists()); self.assertFalse((self.repo / ".agents").exists())

    def test_unknown_tool_is_refused(self):
        r = self.install(str(self.repo), "-t", "bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.repo / ".claude").exists())


@unittest.skipUnless(BASH, "no bash found")
class BashInstaller(InstallerContract, unittest.TestCase):
    flavor = "bash"

    def install(self, *args, home=None, xdg_config=None):
        return bash_install(*args, home=home, xdg_config=xdg_config)


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellInstaller(InstallerContract, unittest.TestCase):
    flavor = "powershell"

    def install(self, *args, home=None, xdg_config=None):
        return ps_install(*args, home=home, xdg_config=xdg_config)

    def test_empty_array_tool_selection_is_refused(self):
        # native PowerShell form: an explicit empty array binds as zero items, unlike an omitted parameter
        script = "& '" + str(ROOT / 'install.ps1') + "' '" + str(self.repo) + "' -Tool @()"
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no tool selected", r.stdout + r.stderr)
        self.assertFalse((self.repo / ".claude").exists()); self.assertFalse((self.repo / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
