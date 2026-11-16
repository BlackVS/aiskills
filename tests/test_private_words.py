"""forge_gates.py's private-word check: the matcher, check-words, the pre-push hook on public and
private remotes, and the rule that no output and no file it writes ever holds a word of the word
file, nor the line the word stands in. The fixture words are made up."""

import json, os, pathlib, shutil, subprocess, sys, unittest

from tests.test_pre_push import BoundaryBase, Repo, fg

WORDS = ("zorbex", "kel-tarn", "vexhub", "vaultkeys/", ".zorbnet")
ALLOWED = "hands.vexhub.example"
WORD_FILE = "# made-up private names\n\nZorbex\nkel-tarn\nvexhub\nvaultkeys/\n.zorbnet\n!" + ALLOWED + "\n"
LINE = "the line that must never be shown"  # every hit line holds this marker too


def leaks(text):
    """The fixture words and hit-line markers found in TEXT, in any case."""
    low = text.lower()
    return [w for w in WORDS + (LINE,) if w in low]


class Matcher(unittest.TestCase):
    def words(self):
        return fg.Words(["Zorbex", "kel-tarn", "a.b"], [ALLOWED])

    def test_whole_words_in_any_case(self):
        w = self.words()
        for text in ("group-zorbex", "zorbex/cli", '"ZORBEX"', "x kel-tarn y", "a.b", "(Kel-Tarn)"):
            self.assertTrue(w.hit(text), text)
        for text in ("zorbexes", "xzorbex", "zorbex_1", "zorbex9", "kel-tarns", "axb", "", "plain text"):
            self.assertFalse(w.hit(text), text)

    def test_an_edge_that_is_not_a_word_character_is_not_bounded(self):
        """A path or host entry keeps matching where a path or a name continues it."""
        w = fg.Words(["vaultkeys/", ".zorbnet", "-x-"])
        for text in ("path vaultkeys/forge/token.txt", "VaultKeys/a", "git.zorbnet", "api.ZORBNET.example",
                     "a-x-b"):
            self.assertTrue(w.hit(text), text)
        # The word-character edge stays bounded: no match runs on into a longer word.
        for text in ("myvaultkeys/a", "git.zorbnets", "vaultkeys", "zorbnet"):
            self.assertFalse(w.hit(text), text)

    def test_an_allowed_literal_covers_only_itself(self):
        w = fg.Words(["vexhub"], [ALLOWED])
        self.assertFalse(w.hit("reviewed by https://HANDS.vexhub.example/run/1"))
        self.assertTrue(w.hit("vexhub"))
        self.assertTrue(w.hit(f"{ALLOWED} and vexhub"))
        self.assertTrue(w.hit("other.vexhub.example"))
        self.assertEqual(w.lines("a\nhands.vexhub.example\nb vexhub\n"), [3])

    def test_scrub_withholds_every_match(self):
        self.assertEqual(self.words().scrub("Zorbex, a.b; zorbexes"), "<private>, <private>; zorbexes")

    def test_the_word_file_format(self):
        path = pathlib.Path(self.id().replace(".", "_") + ".txt")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(WORD_FILE, encoding="utf-8")
        w = fg.read_words(path, "private-word file")
        self.assertEqual((w.entries, w.allowed_literals),
                         (["Zorbex", "kel-tarn", "vexhub", "vaultkeys/", ".zorbnet"], [ALLOWED]))
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(fg.vd.UsageError) as e:
            fg.read_words(path, "private-word file")
        self.assertIn("not UTF-8", str(e.exception))

    def test_remote_hosts_and_private_hosts(self):
        cases = {"https://git.private.example/o/r.git": "git.private.example",
                 "ssh://git@private.example:2222/o/r": "private.example",
                 "git@private.example:o/r.git": "private.example",
                 "C:/repos/r.git": None, "/srv/r.git": None, "file:///srv/r.git": None, "origin": None}
        for url, host in cases.items():
            self.assertEqual(fg.remote_host(url), host, url)
        self.assertTrue(fg.is_private("git.private.example", ["private.example"]))
        self.assertTrue(fg.is_private("PRIVATE.example", ["private.example"]))
        self.assertFalse(fg.is_private("notprivate.example", ["private.example"]))
        self.assertFalse(fg.is_private(None, ["private.example"]))


class WordsRepo(Repo):
    def setUp(self):
        super().setUp()
        self.words = self.tmp / "private-words"
        self.words.write_text(WORD_FILE, encoding="utf-8")
        self.hosts = self.tmp / "private-hosts"
        self.hosts.write_text("private.example\n", encoding="utf-8")
        self.env.update({fg.WORDS_VAR: str(self.words), fg.HOSTS_VAR: str(self.hosts)})

    def add(self, path, content, message="change"):
        f = self.work / path
        f.parent.mkdir(parents=True, exist_ok=True)
        (f.write_bytes if isinstance(content, bytes) else lambda c: f.write_text(c, encoding="utf-8", newline="\n"))(content)
        self.git("add", "--", path)
        return self.commit(message)

    def assertNoLeak(self, *outputs):
        for out in outputs:
            text = out if isinstance(out, str) else json.dumps(out)
            self.assertEqual(leaks(text), [], text)

    def push_reviewed(self, head, *lines, env=None):
        self.record(head)
        code, doc, err = self.pre_push(*(lines or (self.push_line(head),)), env=env)
        self.assertNoLeak(doc, err)
        return code, doc, err


class PrePushWords(WordsRepo):
    def test_an_added_line_is_refused_by_commit_and_place(self):
        head = self.add("notes.txt", f"first\nhost group-zorbex: {LINE}\n")
        code, doc, err = self.push_reviewed(head)
        self.assertEqual((code, doc["outcome"], doc["refs"][0]["status"]), (3, "refused", "refused"))
        self.assertEqual(doc["private_words"]["hits"], [{"commit": head, "in": "line", "path": "notes.txt", "line": 2}])
        self.assertIn(f"a private word in commit {head} notes.txt:2 (not shown)", err)

    def test_a_message_and_a_path_are_new_text_too(self):
        head = self.add("docs/kel-tarn.md", f"{LINE} vexhub\n", message=f"subject\n\nbody {LINE} Zorbex")
        code, doc, err = self.push_reviewed(head)
        self.assertEqual(code, 3)
        hits = doc["private_words"]["hits"]
        self.assertIn({"commit": head, "in": "message", "line": 3}, hits)
        self.assertIn({"commit": head, "in": "path", "path": "path #1"}, hits)
        self.assertIn({"commit": head, "in": "line", "path": "path #1", "line": 1}, hits)

    def test_a_word_added_and_removed_within_the_push_is_still_refused(self):
        first = self.add("a.txt", f"zorbex {LINE}\n")
        self.add("a.txt", "clean\n")
        head = self.commit("later")
        code, doc, _ = self.push_reviewed(head)
        self.assertEqual(code, 3)
        self.assertEqual([h["commit"] for h in doc["private_words"]["hits"]], [first])

    def test_an_allowed_literal_passes_and_the_same_word_outside_it_does_not(self):
        head = self.add("a.txt", f"Review: https://{ALLOWED}/run/7\n")
        self.assertEqual(self.push_reviewed(head)[0], 0)
        head = self.add("a.txt", f"Review: https://{ALLOWED}/run/7 on vexhub, {LINE}\n")
        code, doc, _ = self.push_reviewed(head)
        self.assertEqual((code, len(doc["private_words"]["hits"])), (3, 1))

    def test_a_binary_file_is_read_by_path_only(self):
        head = self.add("blob.bin", b"\x00\x01zorbex\x00")
        code, doc, err = self.push_reviewed(head)
        self.assertEqual((code, doc["private_words"]["binary_files_not_read"]), (0, 1))
        self.assertIn("1 binary file(s) checked by path only", err)
        head = self.add("zorbex.bin", b"\x00\x02")
        self.assertEqual(self.push_reviewed(head)[0], 3)

    def test_history_already_pushed_is_not_read(self):
        self.add("old.txt", f"zorbex {LINE}\n")
        self.git("push", "-q", "origin", "HEAD:main")  # pushed before; no hook in this repository
        head = self.commit("new")
        self.assertEqual(self.push_reviewed(head)[0], 0)

    def test_removing_a_word_already_pushed_is_not_a_hit(self):
        self.add("old.txt", f"zorbex {LINE}\nsecond\n")
        self.git("push", "-q", "origin", "HEAD:main")
        head = self.add("old.txt", "cleaned\nsecond\n")  # the removed line holds the word, the added one not
        self.assertEqual(self.push_reviewed(head)[0], 0)
        # Removed lines do not count toward the new file's line numbers.
        head = self.add("old.txt", f"fresh\nvexhub {LINE}\n")
        self.assertEqual(self.push_reviewed(head)[1]["private_words"]["hits"],
                         [{"commit": head, "in": "line", "path": "old.txt", "line": 2}])

    def test_a_ref_name_is_new_text_and_is_not_shown(self):
        head = self.commit("clean")
        code, doc, err = self.push_reviewed(head, self.push_line(head, "refs/heads/zorbex-fix"))
        self.assertEqual((code, doc["refs"][0]["ref"]), (3, "#1"))
        self.assertIn({"in": "ref", "ref": "#1"}, doc["private_words"]["hits"])

    def test_tags_are_checked_for_words_but_need_no_review(self):
        head = self.commit("clean")
        self.git("tag", "v1")
        self.git("tag", "-a", "v2", "-m", f"release {LINE} for vexhub")
        tag2 = self.git("rev-parse", "v2")
        code, doc, err = self.pre_push(self.push_line(head, "refs/tags/v1"), self.push_line(tag2, "refs/tags/v2"))
        self.assertNoLeak(doc, err)
        self.assertEqual(code, 3)
        self.assertEqual([r["status"] for r in doc["refs"]], ["exempt", "refused"])
        self.assertEqual(doc["private_words"]["hits"], [{"in": "tag", "ref": "refs/tags/v2"}])

    def test_without_a_word_file_the_check_is_off_and_says_so(self):
        head = self.add("a.txt", "zorbex\n")
        env = dict(self.env, **{fg.WORDS_VAR: str(self.tmp / "absent")})
        self.record(head)
        code, doc, err = self.pre_push(self.push_line(head), env=env)
        self.assertEqual((code, doc["private_words"]["state"]), (0, "off"))
        self.assertIn("the private-word check is off", err)

    def test_a_remote_on_a_private_host_is_not_checked_and_not_named(self):
        head = self.add("a.txt", f"zorbex {LINE}\n")
        self.record(head)
        code, doc, err = self.run_fg("pre-push", "origin", "https://git.private.example/o/r.git",
                                     stdin=self.push_line(head))
        self.assertEqual((code, doc["private_words"]["state"]), (0, "not checked"))
        self.assertNotIn("private.example", json.dumps(doc) + err)

    def test_a_skip_covers_the_words_and_its_record_holds_none(self):
        head = self.add("a.txt", f"zorbex {LINE}\n")
        code, doc, err = self.push_reviewed(head, env=dict(self.env, **{fg.SKIP_VAR: "hotfix"}))
        self.assertEqual((code, doc["refs"][0]["status"], doc["refs"][0]["checks"]), (0, "skipped", ["private-words"]))
        record = (self.records() / f"{head}.skip.json").read_text(encoding="utf-8")
        self.assertNoLeak(record)
        self.assertEqual(json.loads(record)["checks"], ["private-words"])

    def test_a_skip_reason_that_holds_a_word_does_not_skip(self):
        head = self.add("a.txt", f"zorbex {LINE}\n")
        code, doc, err = self.push_reviewed(head, env=dict(self.env, **{fg.SKIP_VAR: "ok per Zorbex"}))
        self.assertEqual((code, doc["refs"][0]["status"]), (3, "refused"))
        self.assertIn("the skip reason holds a private word", err)
        self.assertFalse((self.records() / f"{head}.skip.json").exists())


class CheckWords(WordsRepo):
    def test_the_tree_and_commits_are_reported_by_place(self):
        self.add("src/x.py", f"# ok\nHOST = 'kel-tarn'  # {LINE}\n")
        code, doc, err = self.run_fg("check-words", "--tree")
        self.assertNoLeak(doc, err)
        self.assertEqual((code, doc["hits"]), (3, [{"in": "line", "path": "src/x.py", "line": 2}]))
        code, doc, err = self.run_fg("check-words", "--commits", f"{self.base}..HEAD")
        self.assertNoLeak(doc, err)
        self.assertEqual((code, [h["path"] for h in doc["hits"]]), (3, ["src/x.py"]))

    def test_off_without_a_word_file_and_a_usage_error_without_input(self):
        code, doc, err = self.run_fg("check-words", "--tree", "--words", str(self.tmp / "absent"))
        self.assertEqual((code, doc["outcome"]), (0, "off"))
        self.assertEqual(self.run_fg("check-words")[0], 2)


class WordsBoundary(BoundaryBase):
    """Real pushes through the installed hook. Every byte git and the hook print, verbose
    included, and every record written, is searched for every fixture word."""

    def setUp(self):
        super().setUp()
        self.words = self.tmp / "private-words"
        self.words.write_text(WORD_FILE, encoding="utf-8")
        hosts = self.tmp / "private-hosts"
        hosts.write_text("private.example\n", encoding="utf-8")
        self.env.update({fg.WORDS_VAR: str(self.words), fg.HOSTS_VAR: str(hosts)})
        self.assertEqual(self.tool("install-hook").returncode, 0)
        f = self.work / "kel-tarn.txt"
        f.write_text(f"group-zorbex {LINE}\n", encoding="utf-8", newline="\n")
        self.git("add", "--", "kel-tarn.txt")
        self.head = self.commit(f"add notes\n\n{LINE} vexhub")
        self.assertEqual(self.tool("record-review", "--head", self.head, "--level", "medium",
                                   "--verdict", "clean").returncode, 0)

    def git_push(self, *args, env=None):
        p = self.git_in(self.work, "push", "--verbose", *args, check=False, env=env)
        self.assertEqual(leaks(p.stdout + p.stderr), [], p.stdout + p.stderr)
        return p

    def records_text(self):
        d = self.records()
        return "".join(f.read_text(encoding="utf-8") for f in d.glob("*.json")) if d.exists() else ""

    def test_entries_with_a_punctuation_edge_are_refused_in_a_real_push(self):
        """A path entry ending in / and a host entry starting with . match where text continues them."""
        for name, text in (("path", "path vaultkeys/forge/token.txt"), ("host", "see git.zorbnet for it")):
            with self.subTest(entry=name):
                self.git("checkout", "-q", "--detach", self.base)
                f = self.work / f"{name}.txt"
                f.write_text(f"{text} {LINE}\n", encoding="utf-8", newline="\n")
                self.git("add", "--", f.name)
                head = self.commit(f"add {name}")
                self.assertEqual(self.tool("record-review", "--head", head, "--level", "medium",
                                           "--verdict", "clean").returncode, 0)
                p = self.git_push("origin", f"HEAD:refs/heads/{name}")
                self.assertNotEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"a private word in commit {head} {name}.txt:1", p.stderr)

    def test_a_refused_push_shows_no_word_and_no_line(self):
        p = self.git_push("origin", "HEAD:refs/heads/feat")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("a private word in commit", p.stderr)
        self.assertEqual(self.git_in(self.tmp, "--git-dir", str(self.remote), "rev-parse", "--verify", "-q", "feat",
                                     check=False).stdout.strip(), "")

    def fake_ssh_env(self):
        """An ssh that runs git's remote command locally, so a URL can name a private host."""
        fake = self.tmp / "fakessh"
        fake.write_text('#!/bin/sh\nwhile [ $# -gt 1 ]; do shift; done\nexec sh -c "$1"\n', encoding="utf-8", newline="\n")
        return dict(self.env, GIT_SSH_COMMAND=f'sh "{fake.as_posix()}"')

    def private_url(self):
        return "ssh://git@git.private.example/" + self.remote.as_posix().lstrip("/")

    def test_the_same_push_to_a_private_host_goes_through(self):
        p = self.git_push(self.private_url(), "HEAD:refs/heads/feat", env=self.fake_ssh_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        ours = [l for l in p.stderr.splitlines() if l.startswith("pre-push")]
        self.assertIn("pre-push: the remote's host is listed as private", ours)
        self.assertNotIn("private.example", " ".join(ours))

    def test_insteadof_the_hook_judges_the_real_destination(self):
        """git hands the hook the URL as typed ($1) and the rewritten one ($2); $2 decides."""
        # A private-looking alias rewritten to a public (local) destination: checked, refused.
        alias = "https://git.private.example/alias.git"
        self.git("config", f"url.{self.remote.as_posix()}.insteadOf", alias)
        p = self.git_push(alias, "HEAD:refs/heads/feat")
        self.assertNotEqual(p.returncode, 0, p.stderr)
        self.assertIn("a private word in commit", p.stderr)
        # A public-looking alias rewritten to a private host: not checked, goes through.
        alias = "https://public.example/alias.git"
        self.git("config", f"url.{self.private_url()}.insteadOf", alias)
        p = self.git_push(alias, "HEAD:refs/heads/feat", env=self.fake_ssh_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pre-push: the remote's host is listed as private", p.stderr)

    def test_without_a_word_file_the_push_goes_through_with_the_notice(self):
        p = self.git_push("origin", "HEAD:refs/heads/feat", env=dict(self.env, **{fg.WORDS_VAR: str(self.tmp / "absent")}))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("the private-word check is off", p.stderr)

    def test_a_skip_and_its_record_hold_no_word(self):
        p = self.git_push("origin", "HEAD:refs/heads/feat", env=dict(self.env, **{fg.SKIP_VAR: "a person's reason"}))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SKIPPED", p.stderr)
        self.assertIn("private-words", self.records_text())
        self.assertEqual(leaks(self.records_text()), [])
        head = self.commit(f"more {LINE} for zorbex")
        self.assertEqual(self.tool("record-review", "--head", head, "--level", "medium", "--verdict", "clean").returncode, 0)
        p = self.git_push("origin", "HEAD:refs/heads/feat", env=dict(self.env, **{fg.SKIP_VAR: "zorbex said so"}))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(leaks(self.records_text()), [])


if __name__ == "__main__":
    unittest.main()
