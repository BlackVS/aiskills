"""forge_gates.py's pre-push review gate: record-review, pre-push, install-hook and uninstall-hook,
against scratch repositories with a local bare remote. The boundary tests push for real through
the hook git runs (on Windows through Git for Windows' sh), installed from a path with a space."""

import io, json, os, pathlib, shutil, subprocess, sys, tempfile, unittest

from tests.test_install import ROOT

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
sys.path.insert(0, str(ROOT / "skills" / "forge-gates" / "scripts"))
import forge_gates as fg  # noqa: E402

ZERO = "0" * 40
TRAILER = "Claude-Session: https://claude.ai/code/session_01ABCDEFGHIJKLMNOPQRSTUVWX"


@unittest.skipUnless(shutil.which("git"), "no git")
class Repo(unittest.TestCase):
    """A work repository whose origin is a local bare repository holding its main."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pre-push-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.globalcfg = self.tmp / "gitconfig"
        self.globalcfg.write_text("", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k != fg.SKIP_VAR}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.globalcfg),
                        **{fg.WORDS_VAR: str(self.tmp / "no-private-words"),
                           fg.HOSTS_VAR: str(self.tmp / "no-private-hosts")})
        self.remote = self.tmp / "remote.git"
        self.work = self.tmp / "work"
        self.git_in(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git_in(self.tmp, "init", "-q", "-b", "main", str(self.work))
        self.git("remote", "add", "origin", str(self.remote))
        self.base = self.commit("base")
        self.git("push", "-q", "origin", "main")

    def git_in(self, cwd, *args, check=True, env=None):
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                            *args], cwd=cwd, env=env or self.env, capture_output=True, text=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def git(self, *args, **kw):
        p = self.git_in(self.work, *args, **kw)
        return p if not kw.get("check", True) else p.stdout.strip()

    def commit(self, message="change"):
        path = self.tmp / "msg"
        path.write_text(message, encoding="utf-8", newline="\n")
        self.git("commit", "-q", "--allow-empty", "-F", str(path))
        return self.git("rev-parse", "HEAD")

    def run_fg(self, *argv, stdin="", env=None):
        out, err = io.StringIO(), io.StringIO()

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=self.work, env=self.env, **kw)

        code = fg.main(list(argv), env=self.env if env is None else env, stdout=out, stderr=err, run=run,
                       stdin=io.StringIO(stdin))
        return code, json.loads(out.getvalue()), err.getvalue()

    def record(self, head, level="medium", verdict="clean"):
        code, doc, _ = self.run_fg("record-review", "--head", head, "--level", level, "--verdict", verdict)
        self.assertEqual(code, 0, doc)
        return doc

    def push_line(self, head, ref="refs/heads/feat", remote=ZERO):
        return f"{ref} {head} {ref} {remote}\n"

    def pre_push(self, *lines, env=None):
        return self.run_fg("pre-push", "origin", str(self.remote), stdin="".join(lines), env=env)

    def records(self):
        return pathlib.Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "aiskills" / "pre-push"


class PrePushCheck(Repo):
    def test_a_head_without_a_record_is_refused_with_the_command(self):
        head = self.commit()
        code, doc, err = self.pre_push(self.push_line(head))
        self.assertEqual((code, doc["outcome"]), (3, "refused"))
        self.assertEqual(doc["refs"][0]["status"], "refused")
        self.assertIn("no pre-push review is recorded", doc["refs"][0]["detail"])
        self.assertIn(f"record-review --head {head} --level medium --verdict clean", err)
        self.assertIn("after an amend, a rebase or an update from the base", err)

    def test_a_clean_medium_record_lets_the_head_through(self):
        head = self.commit()
        self.record(head)
        code, doc, _ = self.pre_push(self.push_line(head))
        self.assertEqual((code, doc["outcome"], doc["refs"][0]["status"]), (0, "clean", "reviewed"))

    def test_records_that_do_not_count(self):
        head = self.commit()
        for level, verdict, why in (("low", "clean", "below medium"), ("high", "blocker", "found a blocker")):
            self.record(head, level, verdict)
            code, doc, _ = self.pre_push(self.push_line(head))
            self.assertEqual(code, 3, (level, verdict))
            self.assertIn(why, doc["refs"][0]["detail"])
        # A record whose content names another commit.
        other = self.commit("other")
        self.record(other)
        (self.records() / f"{other}.json").replace(self.records() / f"{head}.json")
        code, doc, _ = self.pre_push(self.push_line(head))
        self.assertEqual(code, 3)
        self.assertIn("names another commit", doc["refs"][0]["detail"])

    def test_several_commits_need_only_the_head_recorded(self):
        self.commit("one")
        self.commit("two")
        head = self.commit("three")
        self.record(head)
        self.assertEqual(self.pre_push(self.push_line(head))[0], 0)

    def test_deletions_tags_and_pushes_that_add_no_commit_are_exempt(self):
        head = self.commit()
        code, doc, _ = self.pre_push(
            f"(delete) {ZERO} refs/heads/old {self.base}\n",
            self.push_line(head, "refs/tags/v1.0.0"),
            self.push_line(self.base, "refs/heads/fresh"),  # a new branch at the remote's main
            self.push_line(self.base, "refs/heads/main", remote=head),  # a branch moved back
        )
        self.assertEqual(code, 0, doc)
        self.assertEqual([r["status"] for r in doc["refs"]], ["exempt"] * 4)
        self.assertEqual([r["detail"] for r in doc["refs"]],
                         ["a deletion", "a tag", "it adds no commit the remote lacks", "it adds no commit the remote lacks"])

    def test_a_branch_ahead_of_a_pushed_one_counts_only_its_new_commits(self):
        first = self.commit("first")
        self.record(first)
        self.git("push", "-q", "origin", "HEAD:refs/heads/feat")
        head = self.commit("second")
        self.assertEqual(self.pre_push(self.push_line(head, remote=first))[0], 3)
        self.record(head)
        self.assertEqual(self.pre_push(self.push_line(head, remote=first))[0], 0)

    def test_a_session_link_in_a_new_commit_is_refused_even_when_reviewed(self):
        head = self.commit(f"fix\n\n{TRAILER}\n")
        self.record(head)
        code, doc, err = self.pre_push(self.push_line(head))
        self.assertEqual((code, doc["hits"]), (3, [{"source": f"commit {head}", "line": 3}]))
        self.assertNotIn("claude.ai", err)
        # A commit the remote already holds is not read again.
        self.assertEqual(self.pre_push(self.push_line(self.base, "refs/heads/x"))[0], 0)

    def test_a_push_to_a_url_does_not_read_history_already_pushed(self):
        # origin/main already holds an old commit with a session trailer.
        self.commit(f"old\n\n{TRAILER}\n")
        self.git("push", "-q", "origin", "main")
        head = self.commit("new")
        self.record(head)
        # git passes the URL as the remote's name when the push names a URL.
        out, err = io.StringIO(), io.StringIO()
        code = fg.main(["pre-push", str(self.remote), str(self.remote)], env=self.env, stdout=out, stderr=err,
                       run=lambda cmd, **kw: subprocess.run(cmd, cwd=self.work, env=self.env, **kw),
                       stdin=io.StringIO(self.push_line(head)))
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["hits"], [])

    def test_the_skip_is_printed_recorded_and_named_by_the_next_check(self):
        skipped = self.commit("skipped")
        code, doc, err = self.pre_push(self.push_line(skipped), env=dict(self.env, **{fg.SKIP_VAR: "hotfix, reviewed after"}))
        self.assertEqual((code, doc["refs"][0]["status"]), (0, "skipped"))
        self.assertIn(f"pre-push review gate SKIPPED for {skipped}: hotfix, reviewed after", err)
        skip = json.loads((self.records() / f"{skipped}.skip.json").read_text(encoding="utf-8"))
        self.assertEqual((skip["head"], skip["reason"]), (skipped, "hotfix, reviewed after"))
        # The next push over a range that holds the skipped head names it, as check-links does.
        head = self.commit("next")
        self.record(head)
        code, doc, err = self.pre_push(self.push_line(head))
        self.assertEqual(code, 0)
        self.assertEqual([s["head"] for s in doc["skips"]], [skipped])
        self.assertIn(f"the push of {skipped} skipped the pre-push review gate", err)
        code, doc, err = self.run_fg("check-links", "--commits", f"{self.base}..HEAD")
        self.assertEqual((code, [s["reason"] for s in doc["skips"]]), (0, ["hotfix, reviewed after"]))

    def test_an_empty_skip_value_does_not_skip(self):
        head = self.commit()
        code, doc, err = self.pre_push(self.push_line(head), env=dict(self.env, **{fg.SKIP_VAR: "  "}))
        self.assertEqual((code, doc["refs"][0]["status"]), (3, "refused"))
        self.assertNotIn("SKIPPED", err)
        self.assertFalse((self.records() / f"{head}.skip.json").exists())

    def test_records_stay_out_of_git_status_and_bind_by_full_sha(self):
        head = self.commit()
        doc = self.record("HEAD")
        self.assertEqual(doc["record"]["head"], head)
        self.assertEqual(self.git("status", "--porcelain", "--ignored"), "")
        code, doc, _ = self.run_fg("record-review", "--head", "no-such-rev", "--level", "medium", "--verdict", "clean")
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))


class Install(Repo):
    def hooks(self):
        return pathlib.Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks"))

    def test_a_foreign_pre_push_hook_is_left_as_it_is(self):
        hook = self.hooks() / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        for command in ("install-hook", "uninstall-hook"):
            code, doc, err = self.run_fg(command, "--repo", str(self.work))
            self.assertEqual((code, doc["outcome"]), (3, "refused"), command)
            self.assertIn(str(hook), err)
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_a_global_or_system_hooks_path_needs_the_flag(self):
        shared = self.tmp / "shared hooks"
        self.globalcfg.write_text(f"[core]\n\thooksPath = {shared.as_posix()}\n", encoding="utf-8")
        code, doc, err = self.run_fg("install-hook", "--repo", str(self.work))
        self.assertEqual((code, doc["outcome"]), (3, "refused"))
        self.assertIn("core.hooksPath is set in the global git configuration", err)
        self.assertIn("--shared-hooks-path", err)
        self.assertFalse(shared.exists())
        code, doc, _ = self.run_fg("install-hook", "--repo", str(self.work), "--shared-hooks-path")
        self.assertEqual((code, doc["outcome"]), (0, "installed"))
        self.assertTrue((shared / "pre-push").exists())
        self.assertEqual(self.run_fg("uninstall-hook", "--repo", str(self.work))[1]["outcome"], "removed")

    def test_a_repository_hooks_path_needs_no_flag(self):
        self.git("config", "core.hooksPath", "githooks")
        code, doc, _ = self.run_fg("install-hook", "--repo", str(self.work))
        self.assertEqual((code, doc["outcome"]), (0, "installed"))
        self.assertTrue((self.work / "githooks" / "pre-push").exists())

    def test_install_again_updates_and_uninstall_twice_is_absent(self):
        self.assertEqual(self.run_fg("install-hook", "--repo", str(self.work))[1]["outcome"], "installed")
        self.assertEqual(self.run_fg("install-hook", "--repo", str(self.work))[1]["outcome"], "updated")
        self.assertEqual(self.run_fg("uninstall-hook", "--repo", str(self.work))[1]["outcome"], "removed")
        self.assertEqual(self.run_fg("uninstall-hook", "--repo", str(self.work))[1]["outcome"], "absent")


class BoundaryBase(Repo):
    """A real git push through the hook, installed from a copy of the skill under a path with a space."""

    def setUp(self):
        super().setUp()
        skills = self.tmp / "skills dir" / "skills"
        shutil.copytree(ROOT / "skills" / "forge-gates" / "scripts", skills / "forge-gates" / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "skills" / "verify-delivery", skills / "verify-delivery",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.fg = skills / "forge-gates" / "scripts" / "forge_gates.py"

    def tool(self, *argv):
        return subprocess.run([sys.executable, str(self.fg), *argv], cwd=self.work, env=self.env,
                              capture_output=True, text=True)

    def push(self, env=None):
        return self.git_in(self.work, "push", "origin", "HEAD:refs/heads/feat", check=False, env=env)


class Boundary(BoundaryBase):
    def test_the_hook_refuses_until_the_head_is_recorded(self):
        p = self.tool("install-hook")
        self.assertEqual(p.returncode, 0, p.stderr)
        hook = (self.work / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
        self.assertIn(self.fg.resolve().as_posix(), hook)

        head = self.commit("one")
        p = self.push()
        self.assertNotEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"record-review --head {head}", p.stderr)
        self.assertIn("skills dir", p.stderr)  # the command names the installed copy

        self.assertEqual(self.tool("record-review", "--head", head, "--level", "medium", "--verdict", "clean").returncode, 0)
        p = self.push()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.git_in(self.tmp, "--git-dir", str(self.remote), "rev-parse", "feat").stdout.strip(), head)

        self.commit("two")  # a new commit on top is refused again
        self.assertNotEqual(self.push().returncode, 0)
        p = self.push(env=dict(self.env, **{fg.SKIP_VAR: "a person's reason"}))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pre-push review gate SKIPPED", p.stderr)

        self.commit("three")
        self.assertEqual(self.tool("uninstall-hook").returncode, 0)
        self.assertEqual(self.push().returncode, 0)  # no hook, no gate

    def test_a_moved_script_fails_closed(self):
        self.assertEqual(self.tool("install-hook").returncode, 0)
        shutil.rmtree(self.fg.parent)
        self.commit()
        p = self.push()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("is missing: install forge-gates and run its install-hook again", p.stderr)


if __name__ == "__main__":
    unittest.main()
