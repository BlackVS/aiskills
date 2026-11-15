"""forge_gates.py check-links: no session link in a commit message or a pull request description."""

import io, json, os, shutil, subprocess, sys, tempfile, unittest

from tests.test_install import ROOT

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
sys.path.insert(0, str(ROOT / "skills" / "forge-gates" / "scripts"))
import forge_gates as fg  # noqa: E402

COAUTHOR = "Co-Authored-By: An Agent <agent@example.com>"
TRAILER = "Claude-Session: https://claude.ai/code/session_01ABCDEFGHIJKLMNOPQRSTUVWX"
FOOTER = "Generated with [Claude Code](https://claude.com/claude-code)"


@unittest.skipUnless(shutil.which("git"), "no git")
class CheckLinks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="check-links-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.git("init", "-q")
        self.base = self.commit("base")

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                            *args], cwd=self.dir, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def commit(self, message):
        path = os.path.join(self.dir, "msg")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(message)
        self.git("commit", "-q", "--allow-empty", "-F", path)
        os.remove(path)
        return self.git("rev-parse", "HEAD")

    def file(self, text):
        path = os.path.join(self.dir, "body.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def run_check(self, *argv):
        out, err = io.StringIO(), io.StringIO()

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=self.dir, **kw)

        code = fg.main(["check-links", *argv], env={}, stdout=out, stderr=err, run=run)
        self.assertNotIn("claude.ai", out.getvalue() + err.getvalue(), "the private link is printed")
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_a_trailer_right_after_co_authored_by_is_refused(self):
        sha = self.commit(f"fix: a thing\n\nThe body.\n\n{COAUTHOR}\n{TRAILER}\n")
        code, doc, err = self.run_check("--commits", f"{self.base}..HEAD")
        self.assertEqual((code, doc["outcome"]), (3, "refused"), doc)
        self.assertEqual(doc["hits"], [{"source": f"commit {sha}", "line": 6}])
        self.assertIn(f"commit {sha} line 6", err)

    def test_only_co_authored_by_passes(self):
        self.commit(f"fix: a thing\n\n{COAUTHOR}\n")
        code, doc, _ = self.run_check("--commits", f"{self.base}..HEAD")
        self.assertEqual((code, doc), (0, {"outcome": "clean", "sources": 1, "hits": []}))

    def test_two_adjacent_commits_are_both_found(self):
        first = self.commit(f"one\n\n{COAUTHOR}\n{TRAILER}\n")
        second = self.commit(f"two\n\n{TRAILER}\n")
        code, doc, _ = self.run_check("--commits", f"{self.base}..HEAD")
        self.assertEqual(code, 3)
        self.assertEqual(doc["hits"], [{"source": f"commit {second}", "line": 3}, {"source": f"commit {first}", "line": 4}])

    def test_a_trailer_without_a_url_is_found(self):
        sha = self.commit("fix\n\nclaude-session : 01ABC\n")
        _, doc, _ = self.run_check("--commits", f"{self.base}..HEAD")
        self.assertEqual(doc["hits"], [{"source": f"commit {sha}", "line": 3}])

    def test_a_session_url_in_the_middle_of_a_description_is_found(self):
        body = self.file("## Summary\n\nSee https://claude.ai/code/session_01XYZ for the run.\n\n"
                         f"Done.\n\n{FOOTER}\n")
        code, doc, _ = self.run_check("--file", body)
        self.assertEqual(code, 3)
        self.assertEqual(doc["hits"], [{"source": f"file {body}", "line": 3}])

    def test_two_hits_one_separator_apart(self):
        body = self.file("https://chatgpt.com/c/0123\n---\nhttps://claude.ai/code/session_01XYZ\n")
        _, doc, _ = self.run_check("--file", body)
        self.assertEqual([h["line"] for h in doc["hits"]], [1, 3])

    def test_the_client_footer_and_other_links_pass(self):
        body = self.file(f"## Summary\n\nhttps://github.com/OWNER/REPO/pull/1\nhttps://claude.com/claude-code\n\n{FOOTER}\n")
        self.commit(f"docs\n\n{COAUTHOR}\n")
        code, doc, _ = self.run_check("--commits", f"{self.base}..HEAD", "--file", body)
        self.assertEqual((code, doc["outcome"], doc["sources"]), (0, "clean", 2))

    def test_an_empty_range_is_clean(self):
        code, doc, _ = self.run_check("--commits", "HEAD..HEAD")
        self.assertEqual((code, doc["sources"]), (0, 0))

    def test_usage_errors(self):
        for argv in ([], ["--commits", "--output=/tmp/x"], ["--commits", "no-such-ref..HEAD"],
                     ["--file", os.path.join(self.dir, "missing.md")]):
            with self.subTest(argv=argv):
                code, doc, _ = self.run_check(*argv)
                self.assertEqual((code, doc["outcome"], doc["error"]["kind"]), (2, "error", "usage"), doc)


class SessionLink(unittest.TestCase):
    def test_the_line_test(self):
        for line, want in [(TRAILER, True), ("  Claude-Session: x", True), (COAUTHOR, False), (FOOTER, False),
                           ("see https://claude.ai/code/session_01 here", True), ("https://chat.openai.com/c/1", True),
                           ("no Claude-Session: mid-line without a url", False), ("https://claude.ai", False)]:
            with self.subTest(line=line):
                self.assertEqual(fg.session_link(line), want)


if __name__ == "__main__":
    unittest.main()
