"""Installer tests: both installers put complete skill directories where each
client reads them, on a scratch repo and a scratch home.

Run: python3 -m unittest tests/test_install.py
bash cases run wherever a bash is found (Linux, macOS, Git Bash on Windows);
PowerShell cases run where powershell/pwsh is found (Windows, or pwsh elsewhere).
"""
import datetime, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The managed block's markers, exactly as both installers write them.
BLOCK_MARKERS = ("<!-- aiskills:review-gates start (managed by install.sh, do not edit inside) -->",
                 "<!-- aiskills:review-gates end -->")
CORE = ("architecture-review", "oh-code-review", "oh-technical-writing")
BASH = shutil.which("bash") or next((p for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe") if os.path.exists(p)), None)
PWSH = shutil.which("pwsh") or shutil.which("powershell")
GIT = shutil.which("git")
MANIFEST_FIELDS = ["version", "commit", "skills", "archive_sha256", "installed_at"]


def posix(p):
    return str(p).replace("\\", "/")


def clean_env(xdg_config=None, extra=None):
    # XDG_CONFIG_HOME moves OpenCode's config dir; CI runners set it, so a test sets it only on purpose;
    # the manifest's inputs likewise come only from the test
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "AI_SKILLS_COMMIT", "AI_SKILLS_ARCHIVE_SHA256")}
    if xdg_config is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config)
    env.update(extra or {})
    return env


def bash_install(*args, home=None, xdg_config=None, env=None, root=ROOT):
    e = clean_env(xdg_config, env)
    if home is not None:
        e["HOME"] = posix(home)
    return subprocess.run([BASH, posix(pathlib.Path(root) / "install.sh"), *args], capture_output=True, text=True, env=e)


def ps_install(*args, home=None, xdg_config=None, env=None, root=ROOT):
    script = ""
    if home is not None:
        script += f"Set-Variable -Name HOME -Value '{home}' -Force; "
    script += "& '" + str(pathlib.Path(root) / "install.ps1") + "' " + " ".join(a if re.fullmatch(r"-[A-Za-z]+", a) else "'" + a.replace("'", "''") + "'" for a in args)  # values quoted (empty, comma, space); parameter names bare
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, env=clean_env(xdg_config, env))


def source_commit():
    """HEAD of this checkout, or None when the tests run from an unpacked archive."""
    if not GIT or not (ROOT / ".git").exists():
        return None
    return subprocess.run([GIT, "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def copy_source(dest):
    """A copy of the installer and what it installs, outside any checkout of this repository."""
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return dest


def complete(skill_dir):
    """A skill directory is complete when SKILL.md and every supporting file of the source are there."""
    src = ROOT / "skills" / skill_dir.name
    expected = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    got = sorted(p.relative_to(skill_dir) for p in skill_dir.rglob("*") if p.is_file())
    return expected == got


class InstallerContract:
    """Shared cases; subclasses bind `install` to one installer."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-test-"))
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
        table = {"user": ("--user", "-User"), "prompts": ("-p", "-Prompts"), "agents": ("-a", "-AgentsMd"), "dry": ("--dry-run", "-DryRun")}
        return [table[n][0 if self.flavor == "bash" else 1] for n in names]

    def test_user_level_reaches_opencode_only_when_it_is_installed(self):
        # OpenCode v2 reads only AGENTS.md (no CLAUDE.md fallback) and runs commands from its config dir:
        # both are written when ~/.config/opencode exists, and nothing is created there otherwise
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.home / ".config").exists(), "no OpenCode here: nothing created under ~/.config")
        self.assertIn("aiskills:review-gates start", (self.home / ".claude/CLAUDE.md").read_text(encoding="utf-8"))
        (self.home / ".config/opencode").mkdir(parents=True)
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        agents = self.home / ".config/opencode/AGENTS.md"
        self.assertTrue(agents.is_file(), "global AGENTS.md created for OpenCode")
        self.assertEqual(agents.read_text(encoding="utf-8").count("aiskills:review-gates start"), 1)
        self.assertTrue((self.home / ".config/opencode/commands/code-review.md").is_file(), "prompts installed as OpenCode commands")
        self.assertFalse((self.home / ".config/opencode/prompts").exists())
        # A re-run replaces the managed block with the shipped one and keeps what is outside it.
        start, end = BLOCK_MARKERS
        agents.write_text(f"my notes before\n\n{start}\nstale gates text\n{end}\n\nmy notes after\n", encoding="utf-8")
        r = self.install(*self.flags("user", "prompts", "agents"), home=self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = agents.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(text.count("aiskills:review-gates start"), 1, "replaced, never duplicated")
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
        self.assertEqual(codex.count("aiskills:review-gates start"), 1, "created for Codex")
        self.assertEqual(gemini.count("aiskills:review-gates start"), 1)
        self.assertTrue(gemini.startswith("# my gemini notes\n"), "the user's own notes are kept")

    def test_block_from_before_the_rename_is_replaced_not_doubled(self):
        # Releases before 1.27.0 wrote the block under the set's old name, ai-skills
        old_start = "<!-- ai-skills:review-gates start (managed by install.sh, do not edit inside) -->"
        old_end = "<!-- ai-skills:review-gates end -->"
        agents = self.repo / "AGENTS.md"
        agents.write_text(f"# my notes\n\n{old_start}\nold gates text\n{old_end}\n\nmore notes\n", encoding="utf-8")
        for _ in range(2):
            r = self.install(str(self.repo), *self.flags("agents"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = agents.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("ai-skills:review-gates", text, "the old block is gone")
        self.assertNotIn("old gates text", text)
        self.assertEqual(text.count(BLOCK_MARKERS[0]), 1, "one current block")
        self.assertEqual(text.count(BLOCK_MARKERS[1]), 1)
        self.assertTrue(text.startswith("# my notes\n"))
        self.assertIn("more notes", text)

    def test_project_level_imports_agents_md_for_claude_and_gemini(self):
        # Claude Code reads CLAUDE.md and Gemini CLI GEMINI.md; both resolve "@AGENTS.md" imports
        (self.repo / "GEMINI.md").write_text("# project notes\n", encoding="utf-8")
        for _ in range(2):
            r = self.install(str(self.repo), *self.flags("agents"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.repo / "AGENTS.md").read_text(encoding="utf-8").count("aiskills:review-gates start"), 1)
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
        self.assertIn("aiskills:review-gates start", (xdg / "opencode/AGENTS.md").read_text(encoding="utf-8"))
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

    def manifest(self, dest):
        raw = (dest / ".ai-skills.json").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "no BOM")
        self.assertNotIn(b"\r", raw, "LF line ends")
        m = json.loads(raw.decode("utf-8"))
        self.assertEqual(list(m), MANIFEST_FIELDS, "the fields, in the documented order")
        return m

    def test_manifest_records_what_was_installed(self):
        before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        r = self.install(str(self.repo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for client_dir in (".claude/skills", ".agents/skills"):
            m = self.manifest(self.repo / client_dir)
            self.assertEqual(m["version"], version)
            self.assertEqual(m["commit"], source_commit())
            self.assertEqual(m["skills"], sorted(CORE))
            self.assertIsNone(m["archive_sha256"])
            at = datetime.datetime.strptime(m["installed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            self.assertTrue(before <= at <= datetime.datetime.now(datetime.timezone.utc), m["installed_at"])
        self.assertEqual(sorted(p.name for p in (self.repo / ".claude/skills").iterdir()), [".ai-skills.json", *sorted(CORE)],
                         "no temporary file left behind")

    def test_manifest_is_replaced_and_names_only_this_runs_skills(self):
        self.install(str(self.repo), "-t", "codex")
        r = self.install(str(self.repo), "-t", "codex", "-s", "oh-code-review",
                         env={"AI_SKILLS_ARCHIVE_SHA256": "AB" * 32})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest(self.repo / ".agents/skills")
        self.assertEqual(m["skills"], ["oh-code-review"], "a subset install does not claim the skills it did not install")
        self.assertEqual(m["archive_sha256"], "ab" * 32)
        self.assertEqual(sorted(p.name for p in (self.repo / ".agents/skills").iterdir()), [".ai-skills.json", *sorted(CORE)],
                         "replaced in place, no temporary file left behind")

    def test_manifest_takes_the_commit_from_the_caller_outside_a_checkout(self):
        src = copy_source(self.tmp / "src")
        commit = "0123456789abcdef" * 2 + "01234567"
        r = self.install(str(self.repo), "-t", "claude", root=src, env={"AI_SKILLS_COMMIT": commit})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.manifest(self.repo / ".claude/skills")["commit"], commit)

    @unittest.skipUnless(GIT, "no git found")
    def test_manifest_ignores_an_enclosing_repository(self):
        # unpacked inside some other checkout: that checkout's HEAD is not the source's commit
        outer = self.tmp / "outer"; outer.mkdir()
        def g(*a):
            subprocess.run([GIT, "-C", str(outer), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                           capture_output=True, text=True, check=True)
        g("init", "-q"); g("commit", "-q", "--allow-empty", "-m", "outer")
        src = copy_source(outer / "aiskills")
        r = self.install(str(self.repo), "-t", "claude", root=src)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.manifest(self.repo / ".claude/skills")["commit"])

    def test_manifest_refuses_malformed_inputs(self):
        r = self.install(str(self.repo), "-t", "claude", root=copy_source(self.tmp / "src"),
                         env={"AI_SKILLS_COMMIT": 'abc", "x": "', "AI_SKILLS_ARCHIVE_SHA256": "not-a-digest"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest(self.repo / ".claude/skills")
        self.assertIsNone(m["commit"]); self.assertIsNone(m["archive_sha256"])

    def test_dry_run_writes_no_manifest(self):
        r = self.install(str(self.repo), "-t", "claude", *self.flags("dry"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(".ai-skills.json", r.stdout)
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

    def install(self, *args, home=None, xdg_config=None, env=None, root=ROOT):
        return bash_install(*args, home=home, xdg_config=xdg_config, env=env, root=root)


@unittest.skipUnless(PWSH, "no PowerShell found")
class PowerShellInstaller(InstallerContract, unittest.TestCase):
    flavor = "powershell"

    def install(self, *args, home=None, xdg_config=None, env=None, root=ROOT):
        return ps_install(*args, home=home, xdg_config=xdg_config, env=env, root=root)

    def test_commit_probe_outside_a_checkout_leaves_no_exit_status(self):
        # CI's PowerShell steps end with `exit $LASTEXITCODE`: the failed git probe must not leak into it
        src = copy_source(self.tmp / "src")
        script = "& '" + str(src / "install.ps1") + "' '" + str(self.repo) + "' -Tool claude; exit $LASTEXITCODE"
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, env=clean_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo / ".claude/skills/.ai-skills.json").is_file())

    def test_empty_array_tool_selection_is_refused(self):
        # native PowerShell form: an explicit empty array binds as zero items, unlike an omitted parameter
        script = "& '" + str(ROOT / 'install.ps1') + "' '" + str(self.repo) + "' -Tool @()"
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no tool selected", r.stdout + r.stderr)
        self.assertFalse((self.repo / ".claude").exists()); self.assertFalse((self.repo / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
