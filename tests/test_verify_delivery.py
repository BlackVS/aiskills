"""skills/verify-delivery/verify_delivery.py against recorded API responses
(tests/fixtures/verify_delivery/<forge>/), for GitHub, Gitea and GitLab. No network: a
fake opener serves the fixtures and records every request; no real clock: the
retry sleep is replaced and every timestamp comes from the fixtures.

Run: python3 -m unittest tests/test_verify_delivery.py
"""
import copy, datetime, email.message, io, json, os, pathlib, re, subprocess, sys, tempfile, unittest, urllib.error, urllib.parse
import urllib.request, urllib.response
from unittest import mock

from tests.test_install import ROOT

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
sys.path.insert(0, str(ROOT / "skills" / "verify-delivery"))
import verify_delivery as vd  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "verify_delivery"
HEAD = "3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a"
OLD = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
MERGE = "9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c"
PARENT = "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d"  # the merge commit's first parent: the base it landed on
BASE = "1a3c5e7f9b2d4f6a8c0e2b4d6f8a1c3e5b7d9f0b"  # in a rebase merge, the base under the first rebased commit
C1 = "2d4f6a8c0e1b3d5f7a9c1e3b5d7f9a1c3e5b7d9f"  # the pull request's first commit; HEAD is its second
TOKEN = "fixture-placeholder-not-a-real-token"  # a fake value; the tests check it is never printed
FORGES = {
    "github": {"api": "https://api.github.com", "pr": "https://github.com/acme/widgets/pull/42", "args": [],
               "ci": "check_runs", "env": "GITHUB_TOKEN_FILE"},
    "gitea": {"api": "https://git.example.org/api/v1", "pr": "https://git.example.org/acme/widgets/pulls/42",
              "args": ["--api-base", "https://git.example.org/api/v1"], "ci": "status", "env": "GITEA_TOKEN_FILE",
              # Gitea has no author association: the default reviews need an allowlist there.
              "authors": ["--review-author", "local=author", "--review-author", "external=maintainer"]},
}


class Response:
    def __init__(self, status, body, headers=None):
        self.status, self._body = status, body
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeForge:
    """Serves one forge's fixtures by URL path; records each request."""

    def __init__(self, forge):
        self.forge, self.cfg = forge, FORGES[forge]
        self.data = {p.stem: json.loads(p.read_text()) for p in (FIXTURES / forge).glob("*.json")}
        self.diffs = {p.stem: p.read_text() for p in (FIXTURES / forge).glob("*.diff")}
        self.requests, self.overrides = [], {}

    def route(self, path):
        repo = "/repos/acme/widgets"
        return {f"{repo}/pulls/42": "pull", f"{repo}/issues/42/comments": "issue_comments",
                f"{repo}/pulls/42/reviews": "reviews", f"{repo}/git/commits/{HEAD}": "git_commit_head",
                f"{repo}/git/commits/{MERGE}": "git_commit_merge", f"{repo}/commits/{MERGE}/check-runs": "check_runs",
                f"{repo}/commits/{MERGE}/status": "status",
                # the two diffs of the patch identity rule
                f"{repo}/compare/{PARENT}...{HEAD}": "head_change", f"{repo}/compare/{PARENT}...{MERGE}": "merge_change",
                f"{repo}/compare/{PARENT}...{OLD}": "old_change",
                f"{repo}/pulls/42.diff": "head_change", f"{repo}/git/commits/{MERGE}.diff": "merge_change",
                # a rebase merge of the pull request's two commits: PARENT is the first rebased commit
                f"{repo}/pulls/42/commits": "pull_commits", f"{repo}/git/commits/{PARENT}": "git_commit_rebased",
                f"{repo}/compare/{BASE}...{HEAD}": "head_change", f"{repo}/compare/{BASE}...{OLD}": "old_change",
                f"{repo}/compare/{BASE}...{MERGE}": "rebase_change"}.get(path)

    def __call__(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": dict(req.header_items()),
                              "timeout": timeout})
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path[len(urllib.parse.urlsplit(self.cfg["api"]).path):]
        if path in self.overrides:
            return self.overrides[path](req)
        name = self.route(path)
        if name in self.diffs:
            diff = self.diffs[name]
            return Response(200, diff if isinstance(diff, bytes) else diff.encode())
        if name is None or name not in self.data:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"Not Found"}'))
        return Response(200, json.dumps(self.data[name]).encode())


class Verify:
    forge = None

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="verify-delivery-"))
        self.token_file = self.tmp / "token"
        self.token_file.write_text(TOKEN + "\n")
        self.fake = FakeForge(self.forge)
        self.sleeps = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_vd(self, *extra, env=None, opener=None, authors=None):
        cfg = FORGES[self.forge]
        out, err = io.StringIO(), io.StringIO()
        env = {cfg["env"]: str(self.token_file)} if env is None else env
        authors = cfg.get("authors", []) if authors is None else authors
        code = vd.main(["--pr", cfg["pr"], *cfg["args"], *authors, *extra], env=env, stdout=out, stderr=err,
                       opener=opener or self.fake, sleep=self.sleeps.append)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue(), "the token never appears in output")
        return code, json.loads(out.getvalue())

    def check(self, doc, name):
        return next(c for c in doc["checks"] if c["name"] == name)

    # ---- helpers that edit the recorded responses
    def comments(self):
        return self.fake.data["issue_comments"]

    def drop_external_at_head(self):
        self.fake.data["issue_comments"] = [c for c in self.comments() if not
                                            (c["body"].startswith("[example-bot") and HEAD in c["body"])]

    def ci_fail(self, name_index=0, pending=False):
        if self.forge == "github":
            run = self.fake.data["check_runs"]["check_runs"][name_index]
            run.update(status="in_progress", conclusion=None) if pending else run.update(conclusion="failure")
            return run["name"]
        st = self.fake.data["status"]["statuses"][name_index]
        st["status"] = "pending" if pending else "failure"
        return st["context"]

    # ---- the cases
    def test_confirmed_delivery(self):
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        self.assertEqual([c["status"] for c in doc["checks"]], ["passed"] * 4)
        kinds = [e["kind"] for e in doc["evidence"]]
        self.assertEqual(kinds[:2], ["reviewed_head", "human_merge"])
        self.assertTrue(set(kinds) == {"reviewed_head", "human_merge", "post_merge_ci"})
        self.assertLessEqual(len(doc["evidence"]), 16)
        self.assertTrue(all(len(e["ref"].encode()) <= 512 for e in doc["evidence"]))
        ev = {e["kind"]: e["ref"] for e in doc["evidence"]}
        self.assertTrue(ev["reviewed_head"].endswith(f"/commit/{HEAD}"))
        self.assertTrue(ev["human_merge"].endswith("/42"))
        ci = [e["ref"] for e in doc["evidence"] if e["kind"] == "post_merge_ci"]
        self.assertEqual(len(ci), len(set(ci)), "one ref per CI run, not per job")
        self.assertTrue(all("/job/" not in u for u in ci))
        merge = self.check(doc, "merge")
        self.assertEqual((merge["merged_by"], merge["account_type"], merge["merge_commit_sha"]), ("maintainer", "User", MERGE))
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual({r["review"]: r["status"] for r in reviewed["reviews"]}, {"local": "ready", "external": "ready"})
        self.assertIn("names an older or different head", [i["reason"] for i in reviewed["ignored"]])

    def test_read_only_and_token_in_header_only(self):
        self.run_vd()
        self.assertTrue(self.fake.requests)
        self.assertEqual({r["method"] for r in self.fake.requests}, {"GET"}, "every request is a GET")
        auth = {r["headers"].get("Authorization") for r in self.fake.requests}
        self.assertEqual(len(auth), 1)
        self.assertIn(TOKEN, auth.pop(), "the token goes in the Authorization header")
        self.assertTrue(all(TOKEN not in r["url"] for r in self.fake.requests), "never in a URL")
        self.assertTrue(all(r["timeout"] for r in self.fake.requests), "every request has a timeout")

    def test_older_head_review_that_is_not_ready(self):
        self.drop_external_at_head()  # the external review left names the older head: RETURN_TO_IMPLEMENTATION
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual(reviewed["status"], "failed")
        external = {r["review"]: r for r in reviewed["reviews"]}["external"]
        self.assertEqual((external["status"], external["sha"], external["at_final_head"]), ("not_ready", OLD, False))
        self.assertNotIn("evidence", doc)

    def test_older_head_review_is_ignored_when_the_final_head_is_reviewed(self):
        code, doc = self.run_vd()
        reviewed = self.check(doc, "reviewed_head")
        self.assertIn((OLD, "names an older or different head"), [(i["sha"], i["reason"]) for i in reviewed["ignored"]])
        self.assertTrue(all(r["at_final_head"] for r in reviewed["reviews"]))
        self.assertEqual(reviewed["reviewed_heads"], [HEAD])

    def test_missing_external_review(self):
        self.fake.data["issue_comments"] = [c for c in self.comments() if not c["body"].startswith("[example-bot")]
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertEqual(self.check(doc, "reviewed_head")["ready"], 1)

    def test_review_not_ready_at_head(self):
        for c in self.comments():
            if c["body"].startswith("[example-bot") and HEAD in c["body"]:
                c["body"] = c["body"].replace("READY_FOR_HUMAN_MERGE", "RETURN_TO_IMPLEMENTATION")
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertEqual({r["review"]: r["status"] for r in self.check(doc, "reviewed_head")["reviews"]}["external"], "not_ready")

    def test_review_posted_after_merge_does_not_count(self):
        for c in self.comments():
            if c["body"].startswith("[example-bot") and HEAD in c["body"]:
                c["created_at"] = "2026-09-27T14:00:00Z"  # after the merge at 13:30Z
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertIn("posted after the merge", [i["reason"] for i in self.check(doc, "reviewed_head")["ignored"]])

    def test_review_edited_after_merge_does_not_count(self):
        for c in self.comments():
            if c["body"].startswith("[example-bot") and HEAD in c["body"]:
                c["updated_at"] = "2026-09-27T15:00:00Z"
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertIn("edited after the merge", [i["reason"] for i in self.check(doc, "reviewed_head")["ignored"]])

    def test_token_only_over_https(self):
        base = FORGES[self.forge]["api"].replace("https://", "http://")
        code, doc = self.run_vd("--api-base", base)
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
        self.assertEqual(self.fake.requests, [], "nothing was sent")

    def test_required_count_is_configurable(self):
        self.drop_external_at_head()
        code, doc = self.run_vd("--required-reviews", "1")
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"))

    def test_custom_review_pattern(self):
        self.comments().append({"id": 5, "user": {"login": "qa"}, "created_at": "2026-09-27T13:20:00Z",
                                "html_url": "https://example.invalid/c/5",
                                "body": f"QA passed at {HEAD[:10]}\n\nVERDICT\nREADY_FOR_HUMAN_MERGE\n"})
        code, doc = self.run_vd("--review", r"qa=QA passed at (?P<sha>[0-9a-f]{7,40})", authors=["--review-author", "qa=qa"])
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"))
        self.assertEqual([r["review"] for r in self.check(doc, "reviewed_head")["reviews"]], ["qa"])

    def test_a_review_pattern_must_capture_a_commit_sha(self):
        self.comments().append({"id": 6, "user": {"login": "qa"}, "author_association": "MEMBER",
                                "created_at": "2026-09-27T13:20:00Z", "html_url": "https://example.invalid/c/6",
                                "body": "QA passed at ../../elsewhere\n\nVERDICT\nREADY_FOR_HUMAN_MERGE\n"})
        code, doc = self.run_vd("--review", r"qa=QA passed at (?P<sha>\S*)", authors=["--review-author", "qa=qa"])
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        reviewed = self.check(doc, "reviewed_head")
        self.assertIn("does not name a commit SHA (7 to 40 hex digits)", [i["reason"] for i in reviewed["ignored"]])
        self.assertFalse(any("elsewhere" in r["url"] for r in self.fake.requests))

    def test_bot_merge(self):
        if self.forge == "github":
            self.fake.data["pull"]["merged_by"] = {"login": "merge-helper[bot]", "id": 9, "type": "Bot"}
        else:
            self.fake.data["pull"]["merged_by"] = {"id": -2, "login": "actions"}
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        merge = self.check(doc, "merge")
        self.assertEqual((merge["status"], merge["account_type"]), ("failed", "Bot"))
        code, doc = self.run_vd("--allow-bot-merge")
        self.assertEqual(code, 0, "an explicit opt-in accepts it")

    def test_bot_account_flag_and_merger_allowlist(self):
        code, doc = self.run_vd("--bot-account", "maintainer")
        self.assertEqual((code, self.check(doc, "merge")["account_type"]), (3, "Bot"))
        code, doc = self.run_vd("--merger", "someone-else")
        self.assertEqual(code, 3)
        self.assertIn("allowlist", self.check(doc, "merge")["detail"])
        code, _ = self.run_vd("--merger", "maintainer")
        self.assertEqual(code, 0)

    def test_formal_review_never_counts(self):
        # A formal review can be edited after the merge without a recorded edit time (GitHub's REST
        # API has none), so only comments count: a READY one is listed as ignored, with the reason.
        external = next(c for c in self.comments() if c["body"].startswith("[example-bot") and HEAD in c["body"])
        self.drop_external_at_head()
        self.fake.data["reviews"] = [{"id": 99, "user": dict(external["user"]), "state": "APPROVED", "body": external["body"],
                                      "submitted_at": external["created_at"], "html_url": "https://example.org/review/99",
                                      "author_association": "OWNER"}]
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        reviewed = self.check(doc, "reviewed_head")
        self.assertNotEqual({r["review"]: r["status"] for r in reviewed["reviews"]}["external"], "ready")
        [formal] = [i for i in reviewed["ignored"] if i["url"] == "https://example.org/review/99"]
        self.assertIn("a formal pull-request review, not a comment", formal["reason"])
        self.assertNotIn("evidence", doc)

    def test_merger_and_bot_account_logins_ignore_case(self):
        # logins are case-insensitive on both forges, as --review-author already treats them
        code, _ = self.run_vd("--merger", "MAINTAINER")
        self.assertEqual(code, 0, "an allowlisted merger in other case is accepted")
        code, doc = self.run_vd("--bot-account", "Maintainer")
        self.assertEqual((code, self.check(doc, "merge")["account_type"]), (3, "Bot"))
        self.fake.data["pull"]["merged_by"]["login"] = "MainTainer"  # the forge reports another case
        code, _ = self.run_vd("--merger", "maintainer")
        self.assertEqual(code, 0, "the forge's case does not matter either")
        code, doc = self.run_vd("--bot-account", "maintainer")
        self.assertEqual((code, self.check(doc, "merge")["account_type"]), (3, "Bot"))
        code, doc = self.run_vd("--merger", "someone-else")
        self.assertIn("allowlist", self.check(doc, "merge")["detail"])

    def test_tree_mismatch(self):
        merge = self.fake.data["git_commit_merge"]
        (merge.get("tree") or merge["commit"]["tree"])["sha"] = "1" * 40
        # ...and the merged change is not the reviewed one: it carries an extra line.
        self.fake.diffs["merge_change"] = self.fake.diffs["merge_change"].replace(
            "+    return label\n", "+    return label\n+    print('unreviewed')\n")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertEqual(self.check(doc, "tree_equality")["status"], "failed")

    def test_ci_pending(self):
        self.ci_fail(pending=True)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertEqual(self.check(doc, "post_merge_ci")["status"], "pending")
        self.assertNotIn("evidence", doc)

    def test_no_ci_yet_is_pending(self):
        if self.forge == "github":
            self.fake.data["check_runs"] = {"total_count": 0, "check_runs": []}
        else:
            self.fake.data["status"] = {"state": "", "statuses": []}
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))

    def test_ci_failed(self):
        name = self.ci_fail()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        ci = self.check(doc, "post_merge_ci")
        self.assertEqual(ci["status"], "failed")
        self.assertEqual([r["result"] for r in ci["runs"] if r["name"] == name], ["failed"])

    def test_known_flaky_failure_is_reported_and_not_confirmed(self):
        name = self.ci_fail()
        code, doc = self.run_vd("--known-flaky", name)
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"), "known flaky still blocks")
        ci = self.check(doc, "post_merge_ci")
        self.assertEqual(ci["known_flaky_failures"], [name])
        self.assertEqual([r["result"] for r in ci["runs"] if r["name"] == name], ["failed: known flaky"])
        self.assertIn("not confirmed", ci["detail"])

    def test_failure_outranks_pending(self):
        self.ci_fail(0)
        self.ci_fail(1, pending=True)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))

    def test_open_pr_is_pending(self):
        pr = self.fake.data["pull"]
        pr.update(state="open", merged=False, merged_at=None, merge_commit_sha=None, merged_by=None)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))

    def test_closed_without_merge(self):
        pr = self.fake.data["pull"]
        pr.update(state="closed", merged=False, merged_at=None, merge_commit_sha=None, merged_by=None)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))

    def test_api_unavailable_retries_once(self):
        def down(req, timeout=None):
            self.fake.requests.append({"url": req.full_url})
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        code, doc = self.run_vd(opener=down)
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertEqual(doc["error"]["kind"], "unavailable")
        self.assertEqual(len(self.fake.requests), 2, "exactly one retry")
        self.assertEqual(self.sleeps, [1])

    def test_server_error_is_retryable_without_retry(self):
        path = urllib.parse.urlsplit(FORGES[self.forge]["api"]).path
        self.fake.overrides["/repos/acme/widgets/pulls/42"] = lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"")))
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertEqual(len(self.fake.requests), 1, "no retry on an HTTP error")

    def test_rate_limit_is_retryable(self):
        self.fake.overrides["/repos/acme/widgets/pulls/42"] = lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"")))
        self.assertEqual(self.run_vd()[0], 4)

    def test_token_never_printed_even_when_the_forge_echoes_it(self):
        self.fake.overrides["/repos/acme/widgets/pulls/42"] = lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(f'{{"message":"bad {TOKEN}"}}'.encode())))
        code, doc = self.run_vd()  # run_vd asserts the token is absent from stdout and stderr
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))

    def page(self, path, key, first, second, next_host=None):
        """Serve a listing as two pages; the first page's next link points at
        next_host (default: the API's own host)."""
        api = FORGES[self.forge]["api"]
        base = next_host or api
        seen = []

        def paged(req):
            seen.append(req.full_url)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            items = second if "page" in q else first
            wrapped = {"status": "statuses", "check_runs": "check_runs"}.get(key)
            body = {**self.fake.data[key], wrapped: items} if wrapped else items
            link = {} if "page" in q else {"Link": f'<{base}/repos/acme/widgets{path}?page=2>; rel="next"'}
            return Response(200, json.dumps(body).encode(), link)
        self.fake.overrides["/repos/acme/widgets" + path] = paged
        return seen

    def newer_return_at_head(self):
        """A copy of the external READY review at the head, a minute later and not READY."""
        orig = next(c for c in self.comments() if c["body"].startswith("[example-bot") and HEAD in c["body"])
        later = (vd.when(orig["created_at"]) + datetime.timedelta(minutes=1)).isoformat()
        return {**orig, "id": 99, "created_at": later, "updated_at": later,
                "body": orig["body"].replace("READY_FOR_HUMAN_MERGE", "RETURN_TO_IMPLEMENTATION")}

    def test_pagination_reads_every_page(self):
        seen = self.page("/issues/42/comments", "issue_comments", self.comments(), [self.newer_return_at_head()])
        code, doc = self.run_vd()
        self.assertEqual(len(seen), 2, "both pages were read")
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"), "the newer review on page 2 counts")

    def test_an_unfollowable_next_page_is_never_confirmed(self):
        # Page 2 would hold a newer not-READY review; its link points off the API host.
        seen = self.page("/issues/42/comments", "issue_comments", self.comments(), [self.newer_return_at_head()],
                         next_host="https://elsewhere.example.net")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"], doc["error"]["kind"]), (2, "not_confirmed", "usage"))
        self.assertNotIn("evidence", doc)
        self.assertEqual(len(seen), 1)
        self.assertFalse(any("elsewhere.example.net" in r["url"] for r in self.fake.requests), "never followed off-host")

    def test_ci_on_an_unfollowable_next_page_is_never_confirmed(self):
        github = self.forge == "github"
        key, field = ("check_runs", "check_runs") if github else ("status", "statuses")
        path = f"/commits/{MERGE}/" + ("check-runs" if github else "status")
        runs = self.fake.data[key][field]
        failed = {**runs[0], "id": 99, **({"conclusion": "failure"} if github else {"status": "failure"})}
        self.page(path, key, runs, [failed])
        code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "post_merge_ci")["status"]), (3, "failed"), "page 2 was read")
        self.page(path, key, runs, [failed], next_host="https://elsewhere.example.net")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (2, "not_confirmed"))
        self.assertNotIn("evidence", doc)

    def test_redirects_are_not_followed(self):
        class Redirecting(urllib.request.BaseHandler):
            handler_order = 100  # ahead of the real https handler: nothing leaves the process
            seen = []

            def https_open(self, req):
                self.seen.append((req.full_url, req.get_header("Authorization")))
                headers = email.message.Message()
                headers["Location"] = "http://elsewhere.example.net/steal"
                resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                resp.msg = "Found"
                return resp
        handler = Redirecting()
        code, doc = self.run_vd(opener=vd.default_opener(handler))
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
        self.assertIn("redirect", doc["error"]["message"])
        self.assertEqual(len(handler.seen), 1, "the redirect target was never requested")

    # ---- merged content: tree equality, or the patch identity after a base-only update
    def base_only_update(self):
        """The base moved after the review: the merge commit's tree differs from the head's."""
        merge = self.fake.data["git_commit_merge"]
        (merge.get("commit") or merge)["tree"]["sha"] = "6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e"

    def diff_requests(self):
        return [r for r in self.fake.requests if "/compare/" in r["url"] or r["url"].endswith(".diff")]

    def test_tree_equality_passes_without_reading_diffs(self):
        code, doc = self.run_vd()
        same = self.check(doc, "tree_equality")
        self.assertEqual((code, same["status"], same["rule"]), (0, "passed", "tree_equality"))
        self.assertNotIn("patch_identity", same)
        self.assertEqual(self.diff_requests(), [], "diffs are only read when the trees differ")

    def test_base_only_update_passes_on_patch_identity(self):
        self.base_only_update()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"], same["base_parent_sha"]), ("passed", "patch_identity", PARENT))
        ids = same["patch_identity"]
        self.assertEqual((ids["reviewed_change"], ids["files"]), (ids["merge_change"], 2))
        self.assertNotEqual(self.fake.diffs["head_change"], self.fake.diffs["merge_change"],
                            "the fixtures differ in index lines and hunk positions")
        requests = self.diff_requests()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r["method"] == "GET" for r in requests))
        if self.forge == "github":
            self.assertTrue(all(r["headers"].get("Accept") == "application/vnd.github.diff" for r in requests))

    def assert_changed_line_fails(self):
        self.fake = FakeForge(self.forge)
        self.base_only_update()
        diff = self.fake.diffs["merge_change"]
        self.fake.diffs["merge_change"] = diff.replace("+    label = name.strip()", "+    label = name.lstrip()")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertNotIn("evidence", doc)
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"]), ("failed", None))
        self.assertNotEqual(same["patch_identity"]["reviewed_change"], same["patch_identity"]["merge_change"])

    def test_one_changed_line_fails(self):
        self.assert_changed_line_fails()

    def test_the_same_line_at_another_position_does_not_match(self):
        """An identical added line at a different place in the same file is another change."""
        self.base_only_update()
        diff = self.fake.diffs["merge_change"]
        moved = diff.replace("     name = widget.name\n-    return name\n", "-    return name\n     name = widget.name\n")
        self.assertNotEqual(moved, diff)
        self.fake.diffs["merge_change"] = moved
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertEqual(self.check(doc, "tree_equality")["status"], "failed")

    def test_changed_context_on_the_new_base_does_not_match(self):
        """The conservative outcome: context the base changed next to the hunk is not the reviewed patch."""
        self.base_only_update()
        self.fake.diffs["merge_change"] = self.fake.diffs["merge_change"].replace(
            '     """Render a widget."""\n', '     """Render a widget as text."""\n')
        code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))

    def test_diffs_that_differ_only_in_an_invalid_byte_do_not_match(self):
        self.base_only_update()
        for name, byte in (("head_change", b"\xe9"), ("merge_change", b"\xea")):
            self.fake.diffs[name] = self.fake.diffs[name].encode().replace(b"label = name.strip()",
                                                                           b"label = name.strip() # " + byte)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        ids = self.check(doc, "tree_equality")["patch_identity"]
        self.assertNotEqual(ids["reviewed_change"], ids["merge_change"])
        # The same invalid byte on both sides is the same change: nothing is lost in decoding.
        self.fake.diffs["merge_change"] = self.fake.diffs["merge_change"].replace(b"# \xea", b"# \xe9")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"))

    def test_an_oversized_error_body_is_not_read_in_full(self):
        class Endless(io.RawIOBase):
            """An error body that never ends; counts what was read from it."""
            read_bytes = 0

            def readable(self):
                return True

            def readinto(self, b):
                Endless.read_bytes += len(b)
                b[:] = b"x" * len(b)
                return len(b)
        path = "/repos/acme/widgets/pulls/42"
        self.fake.overrides[path] = lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BufferedReader(Endless())))
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertLessEqual(Endless.read_bytes, vd.MAX_ERROR + io.DEFAULT_BUFFER_SIZE)

    def test_an_oversized_json_response_is_pending(self):
        with mock.patch.object(vd, "MAX_JSON", 64):
            code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertIn("larger than 64 bytes", doc["error"]["message"])

    def test_seeded_fault_identity_that_ignores_content(self):
        """A patch identity that looked only at the file list must let the changed line through."""
        def files_only(diff):
            lines = [l for l in bytes(diff).splitlines() if l.startswith(b"diff --git ")]
            return b"|".join(lines).decode(), len(lines)
        with mock.patch.object(vd, "patch_identity", files_only):
            with self.assertRaises(AssertionError):
                self.assert_changed_line_fails()

    def test_unreadable_diff_is_pending(self):
        merge_diff = "/repos/acme/widgets" + (f"/compare/{PARENT}...{MERGE}" if self.forge == "github"
                                              else f"/git/commits/{MERGE}.diff")
        cases = {
            "not found": lambda: self.fake.diffs.pop("merge_change"),
            "server error": lambda: self.fake.overrides.__setitem__(merge_diff, lambda req: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"")))),
        }
        for name, break_it in cases.items():
            with self.subTest(name):
                self.fake = FakeForge(self.forge)
                self.base_only_update()
                break_it()
                code, doc = self.run_vd()
                self.assertEqual((code, doc["verdict"]), (4, "pending"))
                self.assertNotIn("evidence", doc)
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["rule"]), ("pending", None))
        with self.subTest("larger than the limit"):
            self.fake = FakeForge(self.forge)
            self.base_only_update()
            with mock.patch.object(vd, "MAX_DIFF", 64):
                code, doc = self.run_vd()
            self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (4, "pending"))
            self.assertIn("larger than 64 bytes", self.check(doc, "tree_equality")["detail"])

    def test_no_identity_for_binary_or_parentless_merges(self):
        self.base_only_update()
        self.fake.diffs["merge_change"] += "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
        code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))
        self.assertIn("binary", self.check(doc, "tree_equality")["detail"])
        self.fake = FakeForge(self.forge)
        self.base_only_update()
        self.fake.data["git_commit_merge"].pop("parents")
        code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))
        self.assertEqual(self.diff_requests(), [])

    # ---- a rebase merge of several commits onto a moved base
    def rebase_merge(self):
        """The pull request's two commits were rebased onto a moved base: the merge commit is the
        last rebased commit, so its change against its first parent is that commit's change only."""
        self.base_only_update()
        merge = self.fake.data["git_commit_merge"]
        (merge.get("commit") or merge)["message"] = "Add a render test\n"
        self.fake.diffs["merge_change"] = self.fake.diffs["rebase_last_commit"]

    def rebase_requests(self):
        return [urllib.parse.urlsplit(r["url"]) for r in self.fake.requests
                if f"/git/commits/{PARENT}" in r["url"] or f"/compare/{BASE}..." in r["url"]]

    def test_rebase_merge_of_two_commits_onto_a_moved_base(self):
        self.rebase_merge()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"], same["base_parent_sha"], same["rebased_commits"]),
                         ("passed", "patch_identity", BASE, 2))
        self.assertEqual(same["patch_identity"]["reviewed_change"], same["patch_identity"]["merge_change"])
        compared = [u for u in self.rebase_requests() if "/compare/" in u.path]
        self.assertEqual([u.path.rsplit("/compare/")[1] for u in compared],
                         [f"{BASE}...{HEAD}", f"{BASE}...{MERGE}"] if self.forge == "github" else [f"{BASE}...{MERGE}"])
        if self.forge == "gitea":
            self.assertEqual({u.query for u in compared}, {"output=diff"})

    def test_last_commit_of_a_rebase_alone_is_not_the_reviewed_change(self):
        """Seeded fault: without reading from the rebase's base, the last commit's change never matches."""
        self.rebase_merge()
        with mock.patch.object(vd, "rebase_base", lambda *a: (None, 0)):
            code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))

    def test_a_changed_line_in_a_rebase_merge_fails(self):
        self.rebase_merge()
        self.fake.diffs["rebase_change"] = self.fake.diffs["rebase_change"].replace("name.strip()", "name.lstrip()")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["base_parent_sha"], same["rebased_commits"]), ("failed", BASE, 2))
        self.assertIn("the combined change of the rebase merge's commits is not the reviewed change", same["detail"])

    def pr_commit(self, sha):
        return next(c for c in self.fake.data["pull_commits"] if c["sha"] == sha)

    def test_a_rebase_merge_is_read_whatever_order_the_commits_are_listed_in(self):
        """GitHub lists a pull request's commits oldest first, Gitea newest first (the fixtures)."""
        for order in ("as recorded", "reversed"):
            with self.subTest(order):
                self.fake = FakeForge(self.forge)
                self.rebase_merge()
                if order == "reversed":
                    self.fake.data["pull_commits"].reverse()
                code, doc = self.run_vd()
                self.assertEqual((code, self.check(doc, "tree_equality")["rebased_commits"]), (0, 2))

    def test_a_merge_that_is_not_the_rebased_pull_request_is_not_read_as_one(self):
        merge = lambda: self.fake.data["git_commit_merge"].get("commit") or self.fake.data["git_commit_merge"]
        extra = {"sha": "6a8c0e2b4d6f8a0c2e4b6d8f0a2c4e6b8d0f2a4c", "commit": {"message": "Start the label"},
                 "parents": [{"sha": "8e0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a"}]}
        cases = {
            "the merge commit's message is not the head's": lambda: merge().update(message="Squashed change\n"),
            "an earlier message differs": lambda: self.pr_commit(C1)["commit"].update(message="Another change"),
            "an earlier rebased commit has two parents": lambda: self.fake.data["git_commit_rebased"]["parents"].append(
                {"sha": OLD}),
            "the pull request has one commit": lambda: self.fake.data["pull_commits"].remove(self.pr_commit(C1)),
            "the pull request has more commits than the merge": lambda: (
                self.fake.data["pull_commits"].append(extra), self.pr_commit(C1).update(parents=[{"sha": extra["sha"]}])),
            "the commits are not one line from the head": lambda: self.fake.data["pull_commits"].append(extra),
            "the head is not among the commits": lambda: self.pr_commit(HEAD).update(sha=OLD),
            "the commits are not listed": lambda: self.fake.data.pop("pull_commits"),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                self.fake = FakeForge(self.forge)
                self.rebase_merge()
                edit()
                code, doc = self.run_vd()
                self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["base_parent_sha"]), ("failed", PARENT))
                self.assertNotIn("rebased_commits", same)
                self.assertEqual([u for u in self.rebase_requests() if "/compare/" in u.path], [])

    def test_a_rebase_longer_than_the_bound_is_not_walked(self):
        self.rebase_merge()
        with mock.patch.object(vd, "MAX_REBASED", 1):
            code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))
        self.assertEqual(self.rebase_requests(), [])

    def test_an_unreadable_rebase_is_pending(self):
        self.rebase_merge()
        self.fake.overrides = {"/repos/acme/widgets/pulls/42/commits": lambda req: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}")))}
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (4, "pending"))
        self.assertIn("rebase merge could not be read", self.check(doc, "tree_equality")["detail"])

    @staticmethod
    def unavailable(req):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}"))

    def test_an_unreadable_rebase_walk_is_read_once(self):
        """Two reviewed heads both miss on the first parent; the walk that fails is not repeated."""
        listing = "/repos/acme/widgets/pulls/42/commits"
        reads = {}
        for heads in ("final head only", "final and older head"):
            self.fake = FakeForge(self.forge)
            self.rebase_merge()
            if heads == "final and older head":
                self.reviewed_only_at_older_head()
            self.fake.overrides = {listing: self.unavailable}
            code, doc = self.run_vd()
            self.assertEqual((code, doc["verdict"]), (4, "pending"), heads)
            self.assertEqual(len(self.check(doc, "tree_equality")["reviewed"]), 1 if heads == "final head only" else 2)
            reads[heads] = sum(urllib.parse.urlsplit(r["url"]).path.endswith(listing) for r in self.fake.requests)
        self.assertGreater(reads["final head only"], 0)
        self.assertEqual(reads["final and older head"], reads["final head only"])

    def test_an_unreadable_compare_of_a_rebase_merge_is_pending(self):
        answers = {"unavailable": self.unavailable}
        if self.forge == "github":  # on Gitea a missing compare means a Gitea before 1.22 (a failure that says so)
            answers["not found"] = lambda req: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")))
        for name, answer in answers.items():
            with self.subTest(name):
                self.fake = FakeForge(self.forge)
                self.rebase_merge()
                self.fake.overrides = {f"/repos/acme/widgets/compare/{BASE}...{MERGE}": answer}
                code, doc = self.run_vd()
                self.assertEqual((code, doc["verdict"]), (4, "pending"))
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["rebased_commits"]), ("pending", 2))
                self.assertIn("could not be read", same["detail"])

    def test_a_rebase_merge_reviewed_at_an_older_head(self):
        self.rebase_merge()
        self.reviewed_only_at_older_head()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        rebased = {r["reviewed_head"]: (r["rule"], r.get("rebased_commits")) for r in self.check(doc, "tree_equality")["reviewed"]}
        self.assertEqual(rebased, {HEAD: ("patch_identity", 2), OLD: ("patch_identity", 2)})
        self.assertEqual(sum(f"/git/commits/{PARENT}" in u.path for u in self.rebase_requests()), 1,
                         "the rebase is walked once for every reviewed head")

    def reviewed_only_at_older_head(self):
        """The external review READY at OLD, none at the final head: the branch was only updated
        from its base after that review, so the review still stands for the merged change."""
        self.drop_external_at_head()
        c = next(c for c in self.comments() if OLD in c["body"])
        c["body"] = c["body"].replace("RETURN_TO_IMPLEMENTATION", "READY_FOR_HUMAN_MERGE")

    # ---- who posted the review
    def external_at_head(self):
        return next(c for c in self.comments() if c["body"].startswith("[example-bot") and HEAD in c["body"])

    def forged(self, login="stranger", association="NONE", minutes=5, verdict="READY_FOR_HUMAN_MERGE"):
        """A copy of the external READY review at the head, posted later by another account."""
        orig = self.external_at_head()
        later = (vd.when(orig["created_at"]) + datetime.timedelta(minutes=minutes)).isoformat()
        c = {**orig, "id": 70 + minutes, "user": {"login": login, "id": 700}, "created_at": later, "updated_at": later,
             "html_url": f"https://example.invalid/c/forged-{minutes}",
             "body": orig["body"].replace("READY_FOR_HUMAN_MERGE", verdict)}
        if self.forge == "github":
            c["author_association"] = association
        else:
            c.pop("author_association", None)
        return c

    def replace_external_with_forged(self, **kw):
        forged = self.forged(**kw)
        self.drop_external_at_head()
        self.comments().append(forged)
        return forged

    def assert_forgery_refused(self, association):
        self.fake = FakeForge(self.forge)
        forged = self.replace_external_with_forged(association=association)
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertNotIn("evidence", doc)
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual(reviewed["author_check"], "enabled")
        # Not counted: what remains for "external" is the trusted review of an older head, not READY.
        self.assertNotEqual({r["review"]: r["status"] for r in reviewed["reviews"]}["external"], "ready")
        why = [i for i in reviewed["ignored"] if i["url"] == forged["html_url"] and i["review"] == "external"]
        expected = f"author association {association} not trusted" if self.forge == "github" else "author not allowed"
        self.assertEqual([(i["reason"], i["author"]) for i in why], [(expected, "stranger")])

    def test_forged_review_is_not_counted(self):
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            with self.subTest(association=association):
                self.assert_forgery_refused(association)

    def test_allowlisted_login_passes(self):
        # On GitHub this is also an allowlisted NONE author: the list overrides the association default.
        self.replace_external_with_forged(association="NONE")
        code, doc = self.run_vd(authors=["--review-author", "local=author", "--review-author", "external=Stranger"])
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        external = {r["review"]: r for r in self.check(doc, "reviewed_head")["reviews"]}["external"]
        self.assertEqual((external["author"], external["author_rule"]), ("stranger", "allowlist"))

    def test_allowlist_excludes_accounts_not_on_it(self):
        # The external review is by "maintainer" (OWNER on GitHub); a list without it rejects it.
        code, doc = self.run_vd(authors=["--review-author", "local=author", "--review-author", "external=someone-else"])
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        reviewed = self.check(doc, "reviewed_head")
        self.assertIn(("author not allowed", "maintainer"),
                      [(i["reason"], i["author"]) for i in reviewed["ignored"] if i["review"] == "external"])

    def test_untrusted_newer_comment_does_not_displace_a_trusted_one(self):
        orig = self.external_at_head()
        self.comments().append(self.forged(verdict="RETURN_TO_IMPLEMENTATION"))
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        external = {r["review"]: r for r in self.check(doc, "reviewed_head")["reviews"]}["external"]
        self.assertEqual(external["url"], orig["html_url"])

    def test_trust_any_author_is_explicit_and_reported(self):
        self.replace_external_with_forged()
        code, doc = self.run_vd("--trust-any-author", authors=[])
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual(reviewed["author_check"], "disabled")
        self.assertEqual({r["author_rule"] for r in reviewed["reviews"]}, {"disabled"})

    def test_seeded_fault_without_the_author_check_a_forgery_passes(self):
        """Removing the author check must make the forged-review test fail."""
        with mock.patch.object(vd, "author_verdict", lambda *a, **k: None):
            with self.assertRaises(AssertionError):
                self.assert_forgery_refused("NONE")

    def test_review_author_usage(self):
        for bad in (["--review-author", "external"], ["--review-author", "nosuchreview=someone"],
                    ["--review-author", "external=someone", "--trust-any-author"]):
            with self.subTest(args=bad):
                code, doc = self.run_vd(*bad, authors=[])
                self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))

    def test_usage_errors(self):
        code, doc = self.run_vd(env={})
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"), "no token file")
        code, doc = self.run_vd(env={FORGES[self.forge]["env"]: str(self.tmp / "missing")})
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.tmp), json.dumps(doc), "the token path is not echoed")
        code, doc = self.run_vd("--review", "x=no sha group")
        self.assertEqual(code, 2)
        code, doc = self.run_vd("--required-reviews", "3")
        self.assertEqual(code, 2)
        leaks = (["--token", TOKEN], ["--token-env", TOKEN], [TOKEN], ["--forge", TOKEN], ["--number", TOKEN],
                 ["--review", f"{TOKEN}=x"], ["--review", f"{TOKEN}=("], ["--review", f"x=({TOKEN}"],
                 ["--verdict-pattern", f"({TOKEN}"], ["--review-author", f"{TOKEN}=x"], ["--review-author", TOKEN])
        for leak in leaks:
            with self.subTest(args=leak[0]):
                code, doc = self.run_vd(*leak)  # run_vd asserts the token is not echoed
                self.assertEqual(code, 2, "there is no way to pass a token as an argument")
        # A token can look like a variable name; it is still never repeated.
        ident = "ghp_" + "A1b2C3d4" * 4
        out = io.StringIO()
        code = vd.main(["--pr", FORGES[self.forge]["pr"], *FORGES[self.forge]["args"], "--token-env", ident],
                       env={}, stdout=out, stderr=io.StringIO(), opener=self.fake)
        self.assertEqual(code, 2)
        self.assertNotIn(ident, out.getvalue())

    def test_pr_not_found(self):
        self.fake.data.pop("pull")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))


class GitHub(Verify, unittest.TestCase):
    forge = "github"

    def test_review_at_an_older_head_carries_over_a_base_only_update(self):
        self.reviewed_only_at_older_head()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual(sorted(reviewed["reviewed_heads"]), sorted([HEAD, OLD]))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"]), ("passed", "patch_identity"))
        rules = {r["reviewed_head"]: r["rule"] for r in same["reviewed"]}
        self.assertEqual(rules, {HEAD: "tree_equality", OLD: "patch_identity"})
        self.assertTrue(any(r["url"].endswith(f"/compare/{PARENT}...{OLD}") for r in self.fake.requests))

    def test_review_at_an_older_head_does_not_cover_a_later_change(self):
        self.reviewed_only_at_older_head()
        # What was reviewed at OLD lacks a line the merged change has: a commit after the review.
        self.fake.diffs["old_change"] = self.fake.diffs["old_change"].replace("+    return label\n", "")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["reviewed_head"]), ("failed", OLD))
        self.assertNotIn("evidence", doc)

    def test_association_default_and_missing_association(self):
        code, doc = self.run_vd()
        self.assertEqual({r["author_rule"] for r in self.check(doc, "reviewed_head")["reviews"]}, {"association"})
        forged = self.replace_external_with_forged()
        forged.pop("author_association")
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertIn("author association missing not trusted",
                      [i["reason"] for i in self.check(doc, "reviewed_head")["ignored"]])


class Gitea(Verify, unittest.TestCase):
    forge = "gitea"

    def test_review_at_an_older_head_carries_over_a_base_only_update(self):
        # Gitea 1.27+ serves the compare diff with ?output=diff (observed on gitea.com, 1.27.0+dev,
        # 2026-10-02: its patch identity equals that of git's own three-dot diff)
        self.reviewed_only_at_older_head()
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"]), ("passed", "patch_identity"))
        self.assertEqual({r["reviewed_head"]: r["rule"] for r in same["reviewed"]}, {HEAD: "tree_equality", OLD: "patch_identity"})
        compares = [r for r in self.fake.requests if "/compare/" in r["url"]]
        self.assertEqual([urllib.parse.urlsplit(r["url"]).path.split("/compare/")[1] for r in compares],
                         [f"{PARENT}...{OLD}", f"{PARENT}...{MERGE}"])
        self.assertTrue(all(urllib.parse.urlsplit(r["url"]).query == "output=diff" for r in compares))
        self.assertTrue(all(r["headers"].get("Accept") == "text/plain" for r in compares))

    def test_review_at_an_older_head_does_not_cover_a_later_change(self):
        self.reviewed_only_at_older_head()
        self.fake.diffs["old_change"] = self.fake.diffs["old_change"].replace("+    return label\n", "")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["reviewed_head"]), ("failed", OLD))
        self.assertIn("is not the reviewed change", same["detail"])

    def test_review_at_an_older_head_on_a_gitea_without_the_compare_diff(self):
        # before 1.27 the parameter is ignored and the compare endpoint answers JSON; before 1.22
        # there is no compare endpoint: either way the review cannot be carried over, and says why
        older = {"json": lambda req: Response(200, b'{"total_commits":1,"commits":[]}'),
                 "missing": lambda req: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")))}
        cases = [(name, f"{PARENT}...{OLD}", answer) for name, answer in older.items()]
        cases.append(("json for the merge commit only", f"{PARENT}...{MERGE}", older["json"]))
        for name, compared, answer in cases:
            with self.subTest(name):
                self.reviewed_only_at_older_head()
                self.fake.overrides = {f"/repos/acme/widgets/compare/{compared}": answer}
                code, doc = self.run_vd()
                self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["reviewed_head"]), ("failed", OLD))
                self.assertIn("needs Gitea 1.27 or later", same["detail"])
                self.assertIn("re-review at the final head", same["detail"])
                self.assertNotIn("evidence", doc)

    def test_rebase_merge_on_a_gitea_without_the_compare_diff(self):
        """The combined change of a rebase merge is read only from the compare diff (Gitea 1.27+)."""
        older = {"json": lambda req: Response(200, b'{"total_commits":2,"commits":[]}'),
                 "missing": lambda req: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")))}
        for name, answer in older.items():
            with self.subTest(name):
                self.fake = FakeForge(self.forge)
                self.rebase_merge()
                self.fake.overrides = {f"/repos/acme/widgets/compare/{BASE}...{MERGE}": answer}
                code, doc = self.run_vd()
                self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["rebased_commits"]), ("failed", 2))
                self.assertIn("rebase merge of several commits", same["detail"])
                self.assertIn("needs Gitea 1.27 or later", same["detail"])

    def test_without_an_allowlist_the_reviews_cannot_be_satisfied(self):
        code, doc = self.run_vd(authors=[])
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        self.assertNotIn("evidence", doc)
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual({r["status"] for r in reviewed["reviews"]}, {"unsatisfiable"})
        self.assertTrue(all("allowlist is required on Gitea" in r["detail"] for r in reviewed["reviews"]))
        # Not something waiting helps with: an open PR is refused too, not left pending.
        self.fake.data["pull"].update(state="open", merged=False, merged_at=None, merged_by=None, merge_commit_sha=None)
        code, doc = self.run_vd(authors=[])
        self.assertEqual((code, self.check(doc, "reviewed_head")["status"]), (3, "failed"))

    def test_with_an_allowlist_the_reviews_pass(self):
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"))
        self.assertEqual({r["author_rule"] for r in self.check(doc, "reviewed_head")["reviews"]}, {"allowlist"})

    def test_api_base_is_required(self):
        out = io.StringIO()
        code = vd.main(["--pr", FORGES["gitea"]["pr"]], env={"GITEA_TOKEN_FILE": str(self.token_file)},
                       stdout=out, opener=self.fake)
        self.assertEqual(code, 2)
        self.assertIn("--api-base", json.loads(out.getvalue())["error"]["message"])
        self.assertEqual(self.fake.requests, [])



GITLAB = FIXTURES / "gitlab"
GITLAB_API = "https://gitlab.example.org/api/v4"
GITLAB_PROJECT = "/projects/acme%2Fwidgets"


class FakeGitLab:
    """Serves the exchanges recorded from a live run on gitlab.com (2026-10-05), one fixture per
    merge strategy, with the project, accounts, ids and commit SHAs replaced by placeholders.
    A request is matched on its path and query, as recorded; anything else answers 404."""

    def __init__(self, name):
        self.answers = {x["path"]: x for x in json.loads((GITLAB / f"{name}.json").read_text())}
        self.requests, self.overrides = [], {}

    def body(self, prefix):
        """The recorded answer to the one request whose path and query start with PREFIX."""
        [x] = [x for path, x in self.answers.items() if path.startswith(GITLAB_PROJECT + prefix) or path.startswith(prefix)]
        return x["body"]

    def __call__(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": dict(req.header_items()),
                              "timeout": timeout})
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path[len("/api/v4"):] + ("?" + url.query if url.query else "")
        for prefix, answer in self.overrides.items():
            if path.startswith(GITLAB_PROJECT + prefix) or path.startswith(prefix):
                return answer(req)
        x = self.answers.get(path)
        if x is None:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"404 Not Found"}'))
        return Response(x["status"], json.dumps(x["body"]).encode())


class GitLab(unittest.TestCase):
    """The GitLab adapter (#54) on recorded merge requests: a merge commit and a squash, both
    merged by a project access token's bot account, and a fast-forward merged by a person. Each
    has one review note by the merging account and a green push pipeline on the target branch."""

    REVIEWER = {"merge_commit": "project_1001_bot_example", "squash": "project_1001_bot_example",
                "fast_forward": "maintainer"}

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="verify-delivery-"))
        self.token_file = self.tmp / "token"
        self.token_file.write_text(TOKEN + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, name):
        self.name, self.fake = name, FakeGitLab(name)
        [self.mr] = [x["body"] for path, x in self.fake.answers.items() if re.fullmatch(r".*/merge_requests/\d+", path)]
        return self.mr

    def run_vd(self, *extra, reviewer=None, env=None):
        out = io.StringIO()
        iid = self.mr["iid"]
        args = ["--pr", f"https://gitlab.example.org/acme/widgets/-/merge_requests/{iid}", "--required-reviews", "1",
                "--review-author", f"external={reviewer or self.REVIEWER[self.name]}", *extra]
        env = {"GITLAB_TOKEN_FILE": str(self.token_file)} if env is None else env
        code = vd.main(args, env=env, stdout=out, opener=self.fake, sleep=lambda s: None)
        self.assertNotIn(TOKEN, out.getvalue())
        return code, json.loads(out.getvalue())

    def check(self, doc, name):
        return next(c for c in doc["checks"] if c["name"] == name)

    def paths(self):
        return [urllib.parse.urlsplit(r["url"]).path[len("/api/v4"):] for r in self.fake.requests]

    def test_each_merge_strategy_is_confirmed(self):
        cases = {"merge_commit": ("merge commit", "Bot", ["--allow-bot-merge"]),
                 "squash": ("squash and merge commit", "Bot", ["--allow-bot-merge"]),
                 "fast_forward": ("fast-forward", "User", [])}
        for name, (merged_as, account, extra) in cases.items():
            with self.subTest(name):
                mr = self.load(name)
                code, doc = self.run_vd(*extra)
                self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
                merge = self.check(doc, "merge")
                self.assertEqual((merge["merged_as"], merge["account_type"]), (merged_as, account))
                self.assertEqual(merge["account_type_basis"], "GitLab user bot flag")
                merged = mr["merge_commit_sha"] or mr["squash_commit_sha"] or mr["sha"]
                self.assertEqual(merge["merge_commit_sha"], merged)
                same = self.check(doc, "tree_equality")
                self.assertEqual((same["status"], same["rule"]), ("passed", "tree_equality"))
                self.assertEqual(doc["pr"], {"forge": "gitlab", "repo": "acme/widgets", "number": mr["iid"], "url": mr["web_url"]})
                ev = {e["kind"]: e["ref"] for e in doc["evidence"]}
                self.assertEqual(ev["human_merge"], mr["web_url"])
                self.assertIn("/-/pipelines/", ev["post_merge_ci"])

    def test_a_bot_merge_needs_the_opt_in(self):
        self.load("merge_commit")
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        merge = self.check(doc, "merge")
        self.assertEqual((merge["status"], merge["account_type"]), ("failed", "Bot"))

    def test_without_a_bot_flag_the_merger_is_not_taken_for_a_person(self):
        # the flag is on the user record only, and it needs a token: without it, the account is unknown
        self.load("fast_forward")
        user = self.mr["merge_user"]["id"]
        for name, answer in {"no flag": lambda req: Response(200, json.dumps({"id": user, "username": "maintainer"}).encode()),
                             "no record": lambda req: (_ for _ in ()).throw(
                                 urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")))}.items():
            with self.subTest(name):
                self.fake.overrides = {f"/users/{user}": answer}
                code, doc = self.run_vd()
                self.assertEqual(code, 3)
                merge = self.check(doc, "merge")
                self.assertEqual((merge["account_type"], merge["status"]), ("unknown", "failed"))

    def test_fast_forward_is_its_own_merged_commit(self):
        # no merge commit: the head is what landed, so no compare is needed for the trees
        self.load("fast_forward")
        self.assertIsNone(self.mr["merge_commit_sha"])
        self.run_vd()
        self.assertFalse(any("/compare" in p for p in self.paths()), self.paths())

    def test_requests_are_read_only_with_the_token_in_a_bearer_header(self):
        self.load("merge_commit")
        self.run_vd("--allow-bot-merge")
        self.assertEqual({r["method"] for r in self.fake.requests}, {"GET"})
        self.assertEqual({r["headers"].get("Authorization") for r in self.fake.requests}, {f"Bearer {TOKEN}"})
        self.assertTrue(all(TOKEN not in r["url"] for r in self.fake.requests))
        self.assertTrue(all(r["url"].startswith(GITLAB_API + "/") for r in self.fake.requests), "only the API host")
        self.assertTrue(all(p.startswith((GITLAB_PROJECT + "/", "/users/")) for p in self.paths()))

    def test_only_the_latest_target_branch_push_pipeline_counts(self):
        self.load("fast_forward")
        self.run_vd()
        [ci] = [r["url"] for r in self.fake.requests if "/pipelines" in r["url"]]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(ci).query)
        self.assertEqual((query["ref"], query["source"], query["sort"]), ([self.mr["target_branch"]], ["push"], ["desc"]))
        pipelines = self.fake.body("/pipelines")
        self.assertEqual(len(pipelines), 1, "the branch pipeline on the same commit is not listed")
        for status, (code, result) in {"failed": (3, "failed"), "canceled": (3, "failed"), "running": (4, "pending"),
                                       "manual": (4, "pending"), "skipped": (3, "failed")}.items():
            with self.subTest(status):
                older = {**pipelines[0], "id": 1, "status": "failed"}  # an earlier run: only the latest counts
                latest = {**pipelines[0], "status": status}
                self.fake.overrides = {"/pipelines": lambda req, body=[latest, older]: Response(200, json.dumps(body).encode())}
                got, doc = self.run_vd()
                ci = self.check(doc, "post_merge_ci")
                self.assertEqual((got, [r["result"] for r in ci["runs"]]), (code, [result]))
        self.fake.overrides = {"/pipelines": lambda req: Response(200, b"[]")}
        code, doc = self.run_vd()
        self.assertEqual((code, self.check(doc, "post_merge_ci")["status"]), (4, "pending"))

    def test_system_notes_are_skipped_and_reviews_link_to_their_note(self):
        self.load("squash")
        notes = self.fake.body("/merge_requests/2/notes")
        self.assertTrue(any(n["system"] for n in notes), "the recording has a system note")
        code, doc = self.run_vd("--allow-bot-merge")
        review = {r["review"]: r for r in self.check(doc, "reviewed_head")["reviews"]}["external"]
        [note] = [n for n in notes if not n["system"]]
        self.assertEqual(review["url"], f"{self.mr['web_url']}#note_{note['id']}")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(next(r["url"] for r in self.fake.requests if "/notes" in r["url"])).query)
        self.assertEqual((query["sort"], query["order_by"]), (["asc"], ["created_at"]))

    def test_a_system_note_never_counts_as_a_review(self):
        # GitLab writes system notes itself; one shaped like a review is still not a review
        self.load("fast_forward")
        notes = self.fake.body("/merge_requests/3/notes")
        notes[0]["system"] = True
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertEqual({r["review"]: r["status"] for r in self.check(doc, "reviewed_head")["reviews"]}["external"], "missing")

    def test_a_fast_forward_compares_from_the_requests_base(self):
        # a review of an earlier head, then a fast-forward of a request with two commits: the
        # merged change starts at the request's base, not at the head's first parent
        self.load("fast_forward")
        head, base, earlier = self.mr["sha"], self.mr["diff_refs"]["base_sha"], "e" * 40
        self.fake.body(f"/repository/commits/{head}")["parent_ids"] = ["c" * 40]
        note = self.fake.body("/merge_requests/3/notes")[0]
        note["body"] = note["body"].replace(head, earlier)
        change = {"compare_timeout": False, "diffs": [{"old_path": "README.md", "new_path": "README.md", "a_mode": "100644",
                                                       "b_mode": "100644", "diff": "@@ -1 +1 @@\n-a\n+b\n"}]}
        self.fake.overrides = {"/repository/compare": lambda req: Response(200, json.dumps(change).encode())}
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["rule"], same["base_parent_sha"]), ("patch_identity", base))
        compares = [urllib.parse.parse_qs(urllib.parse.urlsplit(r["url"]).query) for r in self.fake.requests if "/compare" in r["url"]]
        self.assertEqual([(q["from"], q["to"]) for q in compares], [([base], [earlier]), ([base], [head])])

    def test_a_note_edited_after_the_merge_does_not_count(self):
        # observed: editing a note moves updated_at and keeps created_at
        self.load("fast_forward")
        notes = self.fake.body("/merge_requests/3/notes")
        notes[0]["updated_at"] = "2027-01-01T00:00:00.000Z"
        code, doc = self.run_vd()
        self.assertEqual(code, 3)
        self.assertIn("edited after the merge", [i["reason"] for i in self.check(doc, "reviewed_head")["ignored"]])

    def test_without_an_allowlist_the_reviews_cannot_be_satisfied(self):
        self.load("fast_forward")
        out = io.StringIO()
        vd.main(["--pr", f"https://gitlab.example.org/acme/widgets/-/merge_requests/3", "--required-reviews", "1"],
                env={"GITLAB_TOKEN_FILE": str(self.token_file)}, stdout=out, opener=self.fake)
        reviews = self.check(json.loads(out.getvalue()), "reviewed_head")["reviews"]
        self.assertEqual({r["status"] for r in reviews}, {"unsatisfiable"})
        self.assertTrue(all("allowlist is required on GitLab" in r["detail"] for r in reviews))

    def test_open_and_closed_requests(self):
        for state, (code, verdict) in {"opened": (4, "pending"), "closed": (3, "not_confirmed")}.items():
            with self.subTest(state):
                self.load("fast_forward")
                self.mr.update(state=state, merged_at=None, merge_user=None, merged_by=None)
                got, doc = self.run_vd()
                self.assertEqual((got, doc["verdict"]), (code, verdict))

    def base_moved(self):
        """The merge commit request, as if the target had moved after the review: a straight
        compare of the head and the merge commit lists a change, so the trees differ."""
        self.load("merge_commit")
        straight = next(x for p, x in self.fake.answers.items() if "straight=true" in p)
        straight["body"]["diffs"] = [{"old_path": "other.txt", "new_path": "other.txt", "diff": "@@ -1 +1 @@\n-a\n+b\n"}]
        return [x["body"] for p, x in self.fake.answers.items() if "unidiff=true" in p]

    def test_the_patch_identity_carries_a_review_over_a_moved_base(self):
        self.base_moved()
        code, doc = self.run_vd("--allow-bot-merge")
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"), json.dumps(doc, indent=1))
        same = self.check(doc, "tree_equality")
        self.assertEqual((same["status"], same["rule"]), ("passed", "patch_identity"))
        self.assertEqual(same["base_parent_sha"], self.mr["diff_refs"]["base_sha"])
        self.assertEqual(same["patch_identity"]["files"], 7)

    def test_a_merged_change_that_is_not_the_reviewed_one_fails(self):
        reviewed, merged = self.base_moved()
        merged["diffs"][0]["diff"] += "+unreviewed\n"
        code, doc = self.run_vd("--allow-bot-merge")
        self.assertEqual((code, self.check(doc, "tree_equality")["status"]), (3, "failed"))

    def test_a_change_without_a_text_diff_has_no_identity(self):
        cases = {"binary": ("Binary files a/x and b/x differ\n", "binary file"),  # GitLab writes git's line
                 "no text": ("", "no text diff"),
                 "bytes replaced": ("@@ -1 +1 @@\n-a\n+caf\ufffd\n", "bytes replaced")}
        for name, (text, why) in cases.items():
            with self.subTest(name):
                reviewed, merged = self.base_moved()
                merged["diffs"][0]["diff"] = text
                code, doc = self.run_vd("--allow-bot-merge")
                same = self.check(doc, "tree_equality")
                self.assertEqual((code, same["status"]), (3, "failed"))
                self.assertIn(why, same["detail"])

    def test_a_change_past_the_diff_limits_fails_and_is_not_retried(self):
        # compare_timeout is GitLab's overflow flag: lasting, so failed, never pending
        reviewed, merged = self.base_moved()
        for body in [x["body"] for p, x in self.fake.answers.items() if "/compare" in p]:
            body["compare_timeout"] = True
        code, doc = self.run_vd("--allow-bot-merge")
        same = self.check(doc, "tree_equality")
        self.assertEqual((code, doc["verdict"], same["status"]), (3, "not_confirmed", "failed"))
        self.assertIn("diff limits", same["detail"])
        self.assertEqual(self.check(doc, "post_merge_ci")["status"], "passed", "the other checks still report")

    def test_the_rebuilt_diff_has_the_identity_of_the_raw_diff(self):
        # GitLab's compare entries carry the hunks without git's header lines; rebuilt, they hash
        # like the merge request's raw diff (GET .../raw_diffs, recorded with the same placeholders)
        self.load("merge_commit")
        reviewed, _ = [x["body"] for p, x in self.fake.answers.items() if "unidiff=true" in p]
        raw = (GITLAB / "merge_commit_raw.diff").read_bytes()
        self.assertEqual(vd.patch_identity(vd.gitlab_diff(reviewed["diffs"])), vd.patch_identity(raw))
        moved = [dict(d, old_path="a/" + d["old_path"], new_path="a/" + d["new_path"]) for d in reviewed["diffs"]]
        self.assertNotEqual(vd.patch_identity(vd.gitlab_diff(moved)), vd.patch_identity(raw))
        mode = [dict(reviewed["diffs"][1], b_mode="100755")]
        self.assertIn(b"old mode 100644\nnew mode 100755", vd.gitlab_diff(mode))
        rename = [dict(reviewed["diffs"][1], renamed_file=True, new_path="moved.py", diff="")]
        self.assertIn(b"rename from", vd.gitlab_diff(rename), "a pure rename has an identity")
        for broken in (dict(reviewed["diffs"][1], collapsed=True), dict(reviewed["diffs"][1], diff="")):
            with self.assertRaises(ValueError):
                vd.gitlab_diff([broken])

    def test_references(self):
        api = "https://gitlab.example.org/api/v4"
        self.assertEqual(vd.parse_ref("https://gitlab.example.org/acme/widgets/-/merge_requests/3", None, None, None),
                         ("gitlab", api, "acme", "widgets", 3))
        self.assertEqual(vd.parse_ref("https://gitlab.example.org/acme/team/widgets/-/merge_requests/3/diffs?x=1", None, None, None),
                         ("gitlab", api, "acme/team", "widgets", 3))
        self.assertEqual(vd.parse_ref("acme/team/widgets!5", None, "gitlab", api), ("gitlab", api, "acme/team", "widgets", 5))
        self.assertEqual(vd.parse_ref("https://host.example/git/acme/widgets/-/merge_requests/3", None, None,
                                      "https://host.example/git/api/v4"),
                         ("gitlab", "https://host.example/git/api/v4", "acme", "widgets", 3), "a relative URL root")
        for bad, forge in (("acme/team/widgets#5", None), ("https://gitlab.example.org/acme/widgets/-/merge_requests/3", "github"),
                           ("acme/widgets!5", "github"), ("acme/widgets!5", None)):
            with self.subTest(bad), self.assertRaises(vd.UsageError):
                vd.parse_ref(bad, None, forge, None)
        forge = vd.GitLabForge(None, "gitlab", api, "acme/team", "widgets")
        self.assertEqual(forge.repo_path, "/projects/acme%2Fteam%2Fwidgets")
        out = io.StringIO()
        code = vd.main(["--pr", "acme/widgets!5", "--forge", "gitlab"], env={"GITLAB_TOKEN_FILE": str(self.token_file)}, stdout=out)
        self.assertEqual(code, 2)
        self.assertIn("--api-base is required for GitLab without a merge request URL", json.loads(out.getvalue())["error"]["message"])


class References(unittest.TestCase):
    def test_patch_identity_is_canonical(self):
        diff = ("diff --git a/f.txt b/f.txt\nindex 1111111..2222222 100644\n--- a/f.txt\n+++ b/f.txt\n"
                "@@ -1,3 +1,3 @@ top\n a\n-b\n+B\n c\n\\ No newline at end of file\n")
        same = (diff.replace("index 1111111..2222222", "index 3333333333..4444444444")
                    .replace("@@ -1,3 +1,3 @@ top", "@@ -40,3 +40,3 @@ elsewhere")
                    .replace("\n", "\r\n"))
        self.assertEqual(vd.patch_identity(diff), vd.patch_identity(same))
        self.assertEqual(vd.patch_identity(diff), vd.patch_identity(diff.encode()), "text and bytes hash alike")
        self.assertNotEqual(vd.patch_identity(diff.replace("+B\n", "+B\r+x\n")),
                            vd.patch_identity(diff.replace("+B\n", "+B\n+x\n")), "a lone CR is content, not a line end")
        for changed in (diff.replace("+B", "+C"), diff.replace(" a\n", " other context\n"),
                        diff.replace(" a\n-b\n", "-b\n a\n"), diff.replace("-b", "-x"), diff.replace("f.txt", "g.txt"),
                        diff.replace("-b\n+B", "+B\n-b"), diff.replace("-b", "--- b"),
                        diff.replace("\\ No newline at end of file\n", ""),
                        diff.replace("index 1111111..2222222 100644", "old mode 100644\nnew mode 100755\nindex 1..2")):
            self.assertNotEqual(vd.patch_identity(diff), vd.patch_identity(changed), changed)
        self.assertEqual(vd.patch_identity(diff)[1], 1)
        self.assertEqual(vd.patch_identity(diff), vd.patch_identity(diff.replace("\ndiff", "\n\ndiff") + "\n\n"),
                         "blank separator lines do not count")
        for no_identity in ("", "not a diff\n", "diff --git a/p b/p\nBinary files a/p and b/p differ\n"):
            with self.assertRaises(ValueError):
                vd.patch_identity(no_identity)

    def test_parse(self):
        self.assertEqual(vd.parse_ref("https://github.com/acme/widgets/pull/42", None, None, None),
                         ("github", "https://api.github.com", "acme", "widgets", 42))
        self.assertEqual(vd.parse_ref("https://git.example.org/acme/widgets/pulls/7", None, None, "https://git.example.org/api/v1"),
                         ("gitea", "https://git.example.org/api/v1", "acme", "widgets", 7))
        self.assertEqual(vd.parse_ref("acme/widgets#9", None, None, None)[3:], ("widgets", 9))
        self.assertEqual(vd.parse_ref("acme/widgets", 11, "gitea", "https://g.example/api/v1")[0::4], ("gitea", 11))
        self.assertEqual(vd.parse_ref("https://ghe.example.com/acme/widgets/pull/3", None, None, None)[1],
                         "https://ghe.example.com/api/v3")
        self.assertEqual(vd.parse_ref("https://github.com/acme/widgets/pull/42/files?w=1", None, None, None)[4], 42)
        for bad in ("widgets", "acme/widgets", "ftp://x/y/z/pull/1"):
            with self.subTest(bad=bad), self.assertRaises(vd.UsageError):
                vd.parse_ref(bad, None, None, None)

    def test_ci_evidence_falls_back_when_too_many(self):
        runs = [{"url": f"https://example.invalid/actions/runs/{i}/job/{i}"} for i in range(20)]
        self.assertEqual(vd.ci_evidence(runs, "https://example.invalid/commit/abc"), ["https://example.invalid/commit/abc"])

    def test_standalone_script_runs(self):
        script = ROOT / "skills" / "verify-delivery" / "verify_delivery.py"
        env = {k: v for k, v in os.environ.items() if not k.endswith("TOKEN_FILE")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, str(script), "--pr", "acme/widgets#1"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)  # no token file: usage error, before any request
        self.assertEqual(json.loads(r.stdout)["verdict"], "not_confirmed")


if __name__ == "__main__":
    unittest.main()
