"""skills/verify-delivery/verify_delivery.py against recorded API responses
(tests/fixtures/verify_delivery/<forge>/), for GitHub and Gitea. No network: a
fake opener serves the fixtures and records every request; no real clock: the
retry sleep is replaced and every timestamp comes from the fixtures.

Run: python3 -m unittest tests/test_verify_delivery.py
"""
import copy, datetime, email.message, io, json, os, pathlib, subprocess, sys, tempfile, unittest, urllib.error, urllib.parse
import urllib.request, urllib.response

from tests.test_install import ROOT

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
sys.path.insert(0, str(ROOT / "skills" / "verify-delivery"))
import verify_delivery as vd  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "verify_delivery"
HEAD = "3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a"
OLD = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
MERGE = "9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c"
TOKEN = "fixture-placeholder-not-a-real-token"  # a fake value; the tests check it is never printed
FORGES = {
    "github": {"api": "https://api.github.com", "pr": "https://github.com/acme/widgets/pull/42", "args": [],
               "ci": "check_runs", "env": "GITHUB_TOKEN_FILE"},
    "gitea": {"api": "https://git.example.org/api/v1", "pr": "https://git.example.org/acme/widgets/pulls/42",
              "args": ["--api-base", "https://git.example.org/api/v1"], "ci": "status", "env": "GITEA_TOKEN_FILE"},
}


class Response:
    def __init__(self, status, body, headers=None):
        self.status, self._body = status, body
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeForge:
    """Serves one forge's fixtures by URL path; records each request."""

    def __init__(self, forge):
        self.forge, self.cfg = forge, FORGES[forge]
        self.data = {p.stem: json.loads(p.read_text()) for p in (FIXTURES / forge).glob("*.json")}
        self.requests, self.overrides = [], {}

    def route(self, path):
        repo = "/repos/acme/widgets"
        return {f"{repo}/pulls/42": "pull", f"{repo}/issues/42/comments": "issue_comments",
                f"{repo}/pulls/42/reviews": "reviews", f"{repo}/git/commits/{HEAD}": "git_commit_head",
                f"{repo}/git/commits/{MERGE}": "git_commit_merge", f"{repo}/commits/{MERGE}/check-runs": "check_runs",
                f"{repo}/commits/{MERGE}/status": "status"}.get(path)

    def __call__(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": dict(req.header_items()),
                              "timeout": timeout})
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path[len(urllib.parse.urlsplit(self.cfg["api"]).path):]
        if path in self.overrides:
            return self.overrides[path](req)
        name = self.route(path)
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

    def run_vd(self, *extra, env=None, opener=None):
        cfg = FORGES[self.forge]
        out, err = io.StringIO(), io.StringIO()
        env = {cfg["env"]: str(self.token_file)} if env is None else env
        code = vd.main(["--pr", cfg["pr"], *cfg["args"], *extra], env=env, stdout=out, stderr=err,
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

    def test_review_at_older_head_does_not_count(self):
        self.drop_external_at_head()  # the external review left only names the older head
        code, doc = self.run_vd()
        self.assertEqual((code, doc["verdict"]), (3, "not_confirmed"))
        reviewed = self.check(doc, "reviewed_head")
        self.assertEqual(reviewed["status"], "failed")
        self.assertEqual({r["review"]: r["status"] for r in reviewed["reviews"]}["external"], "missing")
        self.assertIn(OLD, [i["sha"] for i in reviewed["ignored"]])
        self.assertNotIn("evidence", doc)

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
        code, doc = self.run_vd("--review", r"qa=QA passed at (?P<sha>[0-9a-f]{7,40})")
        self.assertEqual((code, doc["verdict"]), (0, "confirmed"))
        self.assertEqual([r["review"] for r in self.check(doc, "reviewed_head")["reviews"]], ["qa"])

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

    def test_tree_mismatch(self):
        merge = self.fake.data["git_commit_merge"]
        (merge.get("tree") or merge["commit"]["tree"])["sha"] = "1" * 40
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
                 ["--verdict-pattern", f"({TOKEN}"])
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


class Gitea(Verify, unittest.TestCase):
    forge = "gitea"

    def test_api_base_is_required(self):
        out = io.StringIO()
        code = vd.main(["--pr", FORGES["gitea"]["pr"]], env={"GITEA_TOKEN_FILE": str(self.token_file)},
                       stdout=out, opener=self.fake)
        self.assertEqual(code, 2)
        self.assertIn("--api-base", json.loads(out.getvalue())["error"]["message"])
        self.assertEqual(self.fake.requests, [])


class References(unittest.TestCase):
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
