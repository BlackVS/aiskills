"""skills/forge-gates/scripts/forge_gates.py against recorded-shape API responses for GitHub,
Gitea and GitLab. No network: a fake opener serves the responses and records every request; no
real clock: the wait's clock and sleep are injected; no real GitHub CLI: a fake records how it
was run.

Run: python3 -m unittest tests/test_forge_gates.py
"""
import datetime, io, json, pathlib, shutil, subprocess, sys, tempfile, unittest, urllib.error, urllib.parse

from tests.test_install import ROOT
from tests import test_verify_delivery as tvd  # the module, so its test cases are not collected twice here

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
sys.path.insert(0, str(ROOT / "skills" / "forge-gates" / "scripts"))
import forge_gates as fg  # noqa: E402

vd = fg.vd
HEAD, OLD, TOKEN = tvd.HEAD, tvd.OLD, tvd.TOKEN
TIP = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # the base branch's current tip
NEW = "e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5e"  # a head pushed after HEAD
DIFFS = tvd.FIXTURES / "github"
HEAD_DIFF = (DIFFS / "head_change.diff").read_bytes()
OLD_DIFF = (DIFFS / "old_change.diff").read_bytes()  # the same change on an older base: same identity
OTHER_DIFF = HEAD_DIFF.replace(b"+    return label", b"+    return label or name")  # a different change
CLI_TOKEN = "gh-cli-token-for-this-host-only"
GLOBAL_TOKEN = "synthetic-global-override-token"
START = datetime.datetime(2026, 9, 27, 13, 0, tzinfo=datetime.timezone.utc)  # the wait starts here


def stamp(minutes):
    """An API timestamp MINUTES after the start of the wait (negative: before it)."""
    return (START + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(who, sha, verdict="READY_FOR_HUMAN_MERGE"):
    header = "[oh-code-review high]" if who == "local" else f"[{who} review]"
    return f"{header} reviewed at head {sha}\n\nBLOCKERS\n- None\n\nVERDICT\n{verdict}\n\nRISK\nLOW\n"


class FakeApi:
    """Serves responses by path and query (paging parameters ignored); records each request."""

    def __init__(self, api):
        self.base = urllib.parse.urlsplit(api).path
        self.routes, self.requests = {}, []

    @staticmethod
    def key(path, query):
        return path, frozenset((k, v) for k, v in urllib.parse.parse_qsl(query) if k not in ("per_page", "limit"))

    def set(self, route, body, status=200):
        path, _, query = route.partition("?")
        self.routes[self.key(path, query)] = (status, body)

    def drop(self, route):
        path, _, query = route.partition("?")
        self.routes.pop(self.key(path, query), None)

    def __call__(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": dict(req.header_items()),
                              "timeout": timeout})
        url = urllib.parse.urlsplit(req.full_url)
        status, body = self.routes.get(self.key(url.path[len(self.base):], url.query), (404, {"message": "Not Found"}))
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(data))
        return tvd.Response(200, data)


class FakeGh:
    """Stands in for subprocess.run of the GitHub CLI."""

    def __init__(self, token=CLI_TOKEN, code=0, error=None):
        self.token, self.code, self.error, self.calls = token, code, error, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.code, stdout=self.token + "\n" if self.code == 0 else "", stderr="")


class Clock:
    """time.time and time.sleep for the wait: sleeping moves the clock, then runs the step hooks."""

    def __init__(self):
        self.now, self.sleeps, self.steps = START.timestamp(), [], []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.steps:
            self.steps.pop(0)()


class Gates:
    forge = None
    API = {"github": "https://api.github.com", "gitea": "https://git.example.org/api/v1",
           "gitlab": "https://gitlab.example.org/api/v4"}
    PR = {"github": "https://github.com/acme/widgets/pull/42", "gitea": "https://git.example.org/acme/widgets/pulls/42",
          "gitlab": "https://gitlab.example.org/acme/widgets/-/merge_requests/42"}
    ARGS = {"github": [], "gitea": ["--api-base", "https://git.example.org/api/v1",
                                    "--review-author", "local=author", "--review-author", "external=maintainer"],
            "gitlab": ["--review-author", "local=author", "--review-author", "external=maintainer"]}
    ENV = {"github": "GITHUB_TOKEN_FILE", "gitea": "GITEA_TOKEN_FILE", "gitlab": "GITLAB_TOKEN_FILE"}

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="forge-gates-"))
        self.token_file = self.tmp / "token"
        self.token_file.write_text(TOKEN + "\n")
        self.fake = FakeApi(self.API[self.forge])
        self.clock = Clock()
        self.gh = FakeGh()
        self.repo = "/projects/acme%2Fwidgets" if self.forge == "gitlab" else "/repos/acme/widgets"
        self.comments = []
        self.build()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the recorded responses of one open pull request at HEAD, reviewed and green
    def comment(self, body, author="maintainer", minutes=-30, association="OWNER"):
        n = len(self.comments) + 1
        url = self.PR[self.forge] + (f"#note_{n}" if self.forge == "gitlab" else f"#issuecomment-{n}")
        if self.forge == "gitlab":
            c = {"id": n, "body": body, "created_at": stamp(minutes), "updated_at": stamp(minutes),
                 "author": {"username": author}, "system": False}
        else:
            c = {"id": n, "body": body, "created_at": stamp(minutes), "updated_at": stamp(minutes), "html_url": url,
                 "user": {"login": author}}
            if self.forge == "github":
                c["author_association"] = association
        self.comments.append(c)
        self.publish_comments()
        return c

    def publish_comments(self):
        if self.forge == "gitlab":
            self.fake.set(f"{self.repo}/merge_requests/42/notes?sort=asc&order_by=created_at", self.comments)
        else:
            self.fake.set(f"{self.repo}/issues/42/comments", self.comments)

    def build(self):
        r = self.repo
        if self.forge == "gitlab":
            self.pr = {"iid": 42, "state": "opened", "draft": False, "work_in_progress": False, "sha": HEAD,
                       "target_branch": "main", "title": "Add the widget", "web_url": self.PR["gitlab"],
                       "head_pipeline": {"id": 9, "sha": HEAD, "status": "success",
                                         "web_url": "https://gitlab.example.org/acme/widgets/-/pipelines/9"}}
            self.fake.set(f"{r}/merge_requests/42/versions", [{"head_commit_sha": HEAD}, {"head_commit_sha": OLD}])
            self.fake.set(f"{r}/merge_requests/42/commits", [{"id": HEAD}, {"id": OLD}])
            self.fake.set(f"{r}/repository/branches/main", {"name": "main", "commit": {"id": TIP}})
            self.labels = [{"action": "add", "label": {"name": "review-this"}, "created_at": stamp(-20)}]
            self.fake.set(f"{r}/merge_requests/42/resource_label_events", self.labels)
        else:
            self.pr = {"number": 42, "state": "open", "merged": False, "draft": False, "title": "Add the widget",
                       "html_url": self.PR[self.forge], "head": {"sha": HEAD}, "base": {"ref": "main"}}
            self.fake.set(f"{r}/pulls/42/reviews", [])
            if self.forge == "github":
                self.fake.set(f"{r}/branches/main", {"name": "main", "commit": {"sha": TIP}})
                self.runs = [{"name": "tests", "id": 1, "html_url": "https://github.com/acme/widgets/runs/1",
                              "status": "completed", "conclusion": "success"}]
                self.statuses = [{"context": "ci/legacy", "id": 2, "target_url": "https://ci.example.org/2", "state": "success"}]
                self.fake.set(f"{r}/commits/{HEAD}/check-runs?filter=latest", {"check_runs": self.runs})
                self.fake.set(f"{r}/commits/{HEAD}/status", {"state": "success", "statuses": self.statuses})
                self.labels = [{"event": "labeled", "label": {"name": "review-this"}, "created_at": stamp(-20)}]
                self.fake.set(f"{r}/issues/42/events", self.labels)
            else:
                self.fake.set(f"{r}/branches/main", {"name": "main", "commit": {"id": TIP}})
                self.statuses = [{"context": "ci/tests", "id": 2, "target_url": "https://ci.example.org/2", "status": "success"}]
                self.fake.set(f"{r}/commits/{HEAD}/status", {"state": "success", "statuses": self.statuses})
                self.labels = [{"type": "label", "body": "1", "label": {"name": "review-this"}, "created_at": stamp(-20)}]
                self.fake.set(f"{r}/issues/42/timeline", self.labels)
        self.publish_pr()
        self.set_diff(HEAD, HEAD_DIFF)
        self.set_diff(OLD, OLD_DIFF)
        self.comment(review("example-bot", OLD, "RETURN_TO_IMPLEMENTATION"), minutes=-120)
        self.comment(review("local", HEAD), author="author", minutes=-60, association="MEMBER")
        self.comment(review("example-bot", HEAD), minutes=-10)

    def publish_pr(self):
        self.fake.set(f"{self.repo}/merge_requests/42" if self.forge == "gitlab" else f"{self.repo}/pulls/42", self.pr)

    def set_diff(self, sha, diff, status=200):
        if self.forge == "gitlab":
            # GitLab answers compare entries; the helper rebuilds the diff from them
            text = diff.decode()
            entries = []
            for part in text.split("diff --git ")[1:]:
                path = part.split(" ", 1)[0][2:]
                hunks = part[part.index("@@"):]
                entries.append({"old_path": path, "new_path": path, "a_mode": "100644", "b_mode": "100644",
                                "new_file": "new file mode" in part, "renamed_file": False, "deleted_file": False,
                                "diff": hunks})
                if entries[-1]["new_file"]:
                    entries[-1]["a_mode"] = "0"
            body = {"diffs": entries} if status == 200 else {"message": "error"}
            self.fake.set(f"{self.repo}/repository/compare?from={TIP}&to={sha}&unidiff=true", body, status)
        else:
            route = f"{self.repo}/compare/{TIP}...{sha}" + ("?output=diff" if self.forge == "gitea" else "")
            self.fake.set(route, diff, status)

    def external_at_head(self):
        return self.comments[2]

    # ---- running the script
    def run_fg(self, command, *extra, env=None, args=None):
        out, err = io.StringIO(), io.StringIO()
        env = {self.ENV[self.forge]: str(self.token_file)} if env is None else env
        argv = [command, "--pr", self.PR[self.forge], *(self.ARGS[self.forge] if args is None else args), *extra]
        code = fg.main(argv, env=env, stdout=out, stderr=err, opener=self.fake, sleep=self.clock.sleep,
                       clock=self.clock, run=self.gh)
        text = out.getvalue() + err.getvalue()
        for secret in (TOKEN, CLI_TOKEN, GLOBAL_TOKEN):
            self.assertNotIn(secret, text, "no token ever appears in the output")
        self.assertEqual(err.getvalue().count("\n"), 1, "one summary line on stderr")
        self.assertEqual({r["method"] for r in self.fake.requests} - {"GET"}, set(), "every request is a GET")
        return code, json.loads(out.getvalue())

    def gates(self, *extra, **kw):
        return self.run_fg("gates", *extra, **kw)

    def wait(self, *extra, head=HEAD, **kw):
        return self.run_fg("wait-review", "--head", head, "--interval", "60", "--timeout", "600", *extra, **kw)

    @staticmethod
    def check(doc, name):
        return next(c for c in doc["checks"] if c["name"] == name)

    def review_of(self, doc, name="external"):
        return next(r for r in self.check(doc, "reviews")["reviews"] if r["review"] == name)


class GatesContract(Gates):
    """The cases every forge shares."""

    def test_ready_head(self):
        code, doc = self.gates()
        self.assertEqual((code, doc["status"], doc["ready"]), (0, "ready", True), json.dumps(doc, indent=1))
        self.assertEqual([c["status"] for c in doc["checks"]], ["passed"] * 4)
        self.assertEqual(doc["pr"]["head_sha"], HEAD)
        reviews = self.check(doc, "reviews")
        self.assertEqual({r["review"]: (r["status"], r["at_final_head"]) for r in reviews["reviews"]},
                         {"local": ("ready", True), "external": ("ready", True)})
        self.assertFalse(any("/compare" in r["url"] for r in self.fake.requests), "no identity is read for the head's own review")

    def test_a_newer_review_of_an_older_head_does_not_displace_the_heads(self):
        self.comment(review("example-bot", OLD, "RETURN_TO_IMPLEMENTATION"), minutes=-5)
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (0, "ready"))

    def test_a_later_not_ready_review_of_the_head_supersedes_an_earlier_ready(self):
        self.comment(review("example-bot", HEAD, "RETURN_TO_IMPLEMENTATION"), minutes=-5)
        code, doc = self.gates()
        self.assertEqual((code, doc["status"], self.review_of(doc)["status"]), (3, "not_ready", "not_ready"))

    def test_stale_review_after_a_new_commit(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        self.set_diff(OLD, OTHER_DIFF)
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, doc["status"], r["status"], r["sha"]), (3, "not_ready", "stale", OLD), json.dumps(r))
        self.assertNotEqual(r["change_identity"]["reviewed"], r["change_identity"]["current"])
        self.assertNotIn("patch_id", json.dumps(doc), "the forge digest is never called a patch-id")

    def test_review_carried_over_a_base_only_update(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, doc["status"], r["status"], r.get("carried_from")), (0, "ready", "ready", OLD),
                         json.dumps(doc, indent=1))
        self.assertEqual(r["change_identity"]["reviewed"], r["change_identity"]["current"])
        self.assertEqual(self.check(doc, "reviews")["base_tip"], TIP)

    def test_identity_that_cannot_be_read_is_unknown_not_ready(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        self.set_diff(OLD, b'{"message": "error"}', status=500)
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, doc["status"], r["status"]), (4, "pending", "unknown"), json.dumps(r))
        self.assertFalse(doc["ready"])

    def test_an_older_head_named_by_an_abbreviation_is_not_carried(self):
        self.external_at_head()["body"] = review("example-bot", OLD[:12])
        self.publish_comments()
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (3, "not_ready"))
        self.assertFalse(any("/compare" in r["url"] for r in self.fake.requests))

    def test_missing_review(self):
        del self.comments[2]
        self.publish_comments()
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (3, "not_ready"))

    def test_draft(self):
        self.pr["draft"] = True
        self.publish_pr()
        code, doc = self.gates()
        self.assertEqual((code, doc["status"], self.check(doc, "draft")["draft"]), (3, "not_ready", True))

    def test_closed_and_merged(self):
        for state in ("closed", "merged"):
            with self.subTest(state):
                if self.forge == "gitlab":
                    self.pr["state"] = state
                else:
                    self.pr.update(state="closed", merged=state == "merged")
                self.publish_pr()
                code, doc = self.gates()
                self.assertEqual((code, self.check(doc, "state")["state"]), (3, state))

    def test_token_only_in_the_header_and_only_to_the_api(self):
        self.gates()
        self.assertTrue(self.fake.requests)
        self.assertTrue(all(r["url"].startswith(self.API[self.forge] + "/") for r in self.fake.requests))
        self.assertTrue(all(TOKEN in r["headers"].get("Authorization", "") for r in self.fake.requests))
        self.assertTrue(all(TOKEN not in r["url"] and r["timeout"] for r in self.fake.requests))
        self.assertEqual(self.gh.calls, [], "a token file wins over the CLI")


class WaitContract(Gates):
    """wait-review on every forge."""

    def test_finds_the_review_by_any_abbreviation(self):
        for length in (7, 8, 12, 40):
            with self.subTest(length=length):
                self.external_at_head()["body"] = review("example-bot", HEAD[:length].upper() if length == 8 else HEAD[:length])
                self.publish_comments()
                code, doc = self.wait()
                self.assertEqual((code, doc["outcome"]), (0, "review_found"), json.dumps(doc))
                self.assertEqual((doc["review"]["abbreviated"], doc["review"]["verdict"], doc["review"]["ready_verdict"]),
                                 (length < 40, "READY_FOR_HUMAN_MERGE", True))
                self.assertEqual(doc["polls"], 1)

    def test_a_not_ready_answer_is_still_an_answer(self):
        self.external_at_head()["body"] = review("example-bot", HEAD, "RETURN_TO_IMPLEMENTATION")
        self.publish_comments()
        code, doc = self.wait()
        self.assertEqual((code, doc["review"]["verdict"], doc["review"]["ready_verdict"]), (0, "RETURN_TO_IMPLEMENTATION", False))

    def test_waits_until_the_review_arrives(self):
        del self.comments[2]
        self.publish_comments()
        self.clock.steps = [lambda: None, lambda: self.comment(review("example-bot", HEAD[:7]), minutes=2)]
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["polls"]), (0, "review_found", 3))
        self.assertEqual(self.clock.sleeps, [60, 60])

    def test_ignores_another_head_and_times_out(self):
        del self.comments[2]
        self.publish_comments()
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))
        self.assertEqual(sum(self.clock.sleeps), 600)
        self.assertEqual(doc["polls"], 11)

    def test_correlated_failure_ends_the_wait(self):
        del self.comments[2]
        self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=-5)
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["correlation"]), (3, "reviewer_failed", "label"), json.dumps(doc))
        self.assertTrue(doc["failure"]["url"])

    def test_failure_before_the_latest_request_label_does_not_count(self):
        del self.comments[2]
        self.comment("⚠️ The external review could not run.", minutes=-25)  # the label was added at -20
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))

    def test_failure_naming_another_head_does_not_count(self):
        del self.comments[2]
        self.comment(f"⚠️ The external review could not run for {NEW[:8]}.", minutes=-5)
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))

    def test_without_label_events_the_wait_start_is_used(self):
        del self.comments[2]
        self.labels.clear()
        self.comment("⚠️ The external review could not run.", minutes=-5)  # before the wait: not this request's
        self.clock.steps = [lambda: self.comment("⚠️ The external review could not run.", minutes=1)]
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["correlation"], doc["polls"]), (3, "reviewer_failed", "wait_start", 2))

    def test_moved_head(self):
        del self.comments[2]
        self.publish_comments()
        if self.forge == "gitlab":
            self.pr["sha"] = NEW
        else:
            self.pr["head"]["sha"] = NEW
        self.publish_pr()
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["current_head"]), (3, "head_moved", NEW))

    def test_closed_and_merged(self):
        del self.comments[2]
        self.publish_comments()
        for state in ("closed", "merged"):
            with self.subTest(state):
                if self.forge == "gitlab":
                    self.pr["state"] = state
                else:
                    self.pr.update(state="closed", merged=state == "merged")
                self.publish_pr()
                code, doc = self.wait()
                self.assertEqual((code, doc["outcome"]), (3, state))

    def test_keeps_waiting_through_an_unavailable_forge(self):
        route = (f"{self.repo}/merge_requests/42/notes?sort=asc&order_by=created_at" if self.forge == "gitlab"
                 else f"{self.repo}/issues/42/comments")
        self.fake.set(route, {"message": "down"}, status=503)
        self.clock.steps = [self.publish_comments]
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["polls"]), (0, "review_found", 2))
        self.fake.set(route, {"message": "down"}, status=503)
        code, doc = self.wait("--timeout", "120")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))
        self.assertIn("503", doc["last_error"])

    def test_usage_errors_before_the_wait(self):
        for extra, why in ((["--head", HEAD[:12]], "40-digit"), (["--interval", "10"], "at least 30"),
                           (["--wait-for", "nobody"], "not configured")):
            with self.subTest(why):
                self.fake.requests.clear()
                code, doc = self.run_fg("wait-review", *(["--head", HEAD] if extra[0] != "--head" else []), *extra)
                self.assertEqual((code, doc["outcome"]), (2, "error"))
                self.assertIn(why, doc["error"]["message"])
                self.assertEqual(self.fake.requests, [], "nothing is read")
                self.assertEqual(self.clock.sleeps, [])


class GitHubGates(GatesContract, unittest.TestCase):
    forge = "github"

    def test_ci_counts_check_runs_and_statuses(self):
        code, doc = self.gates()
        self.assertEqual([r["name"] for r in self.check(doc, "ci")["runs"]], ["tests", "ci/legacy"])
        for change, expected in (("run pending", (4, "pending")), ("run failed", (3, "failed")),
                                 ("status failed", (3, "failed")), ("status pending", (4, "pending")),
                                 ("absent", (4, "pending"))):
            with self.subTest(change):
                self.build_ci(change)
                code, doc = self.gates()
                ci = self.check(doc, "ci")
                self.assertEqual((code, ci["status"]), expected, json.dumps(ci))
                if change == "absent":
                    self.assertIn("no CI results", ci["detail"])

    def build_ci(self, change):
        runs = [dict(r) for r in self.runs]
        statuses = [dict(s) for s in self.statuses]
        if change == "run pending":
            runs[0].update(status="in_progress", conclusion=None)
        elif change == "run failed":
            runs[0]["conclusion"] = "failure"
        elif change == "status failed":
            statuses[0]["state"] = "error"
        elif change == "status pending":
            statuses[0]["state"] = "pending"
        elif change == "absent":
            runs, statuses = [], []
        self.fake.set(f"{self.repo}/commits/{HEAD}/check-runs?filter=latest", {"check_runs": runs})
        self.fake.set(f"{self.repo}/commits/{HEAD}/status", {"state": "pending", "statuses": statuses})

    def test_known_flaky_failure_still_fails(self):
        self.build_ci("run failed")
        code, doc = self.gates("--known-flaky", "tests")
        self.assertEqual((code, self.check(doc, "ci")["runs"][0]["result"]), (3, "failed: known flaky"))

    def test_review_naming_the_head_by_an_abbreviation_is_not_ready(self):
        self.external_at_head()["body"] = review("example-bot", HEAD[:12])
        self.publish_comments()
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, r["status"]), (3, "not_ready"))
        self.assertIn("full 40-digit SHA", r["detail"])

    def test_untrusted_author_never_counts(self):
        self.comment(review("example-bot", HEAD, "RETURN_TO_IMPLEMENTATION"), author="drive-by", minutes=-1,
                     association="NONE")
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (0, "ready"))
        self.external_at_head()["author_association"] = "CONTRIBUTOR"
        self.publish_comments()
        code, doc = self.gates()
        self.assertEqual(code, 3)

    def test_wait_ignores_an_untrusted_comment(self):
        del self.comments[2]
        self.comment(review("example-bot", HEAD), author="drive-by", minutes=-1, association="NONE")
        self.comment("⚠️ The external review could not run.", author="drive-by", minutes=-1, association="NONE")
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))

    def test_a_variant_request_label_counts(self):
        del self.comments[2]
        self.labels.append({"event": "labeled", "label": {"name": "review-this:other-model"}, "created_at": stamp(-2)})
        self.comment("⚠️ The external review could not run.", minutes=-5)  # after review-this, before the variant
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"))


class GitHubWait(WaitContract, unittest.TestCase):
    forge = "github"


class GiteaGates(GatesContract, unittest.TestCase):
    forge = "gitea"

    def test_title_prefix_marks_a_draft_without_the_field(self):
        del self.pr["draft"]
        for title, draft in (("Add the widget", False), ("WIP: Add the widget", True), ("[wip] Add the widget", True)):
            with self.subTest(title):
                self.pr["title"] = title
                self.publish_pr()
                code, doc = self.gates()
                self.assertEqual((code, self.check(doc, "draft")["draft"]), (3 if draft else 0, draft))

    def test_draft_prefix_replaces_the_defaults(self):
        del self.pr["draft"]
        for title, draft in (("WIP: Add the widget", False), ("Draft: Add the widget", True)):
            with self.subTest(title):
                self.pr["title"] = title
                self.publish_pr()
                code, doc = self.gates("--draft-prefix", "Draft:")
                self.assertEqual(self.check(doc, "draft")["draft"], draft)

    def test_an_older_gitea_gives_unknown(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        self.set_diff(OLD, json.dumps({"commits": []}).encode())  # before 1.27 ?output=diff is ignored: JSON
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, r["status"]), (4, "unknown"))
        self.assertIn("1.27", r["detail"])

    def test_ci_from_statuses(self):
        self.statuses[0]["status"] = "failure"
        self.fake.set(f"{self.repo}/commits/{HEAD}/status", {"state": "failure", "statuses": self.statuses})
        code, doc = self.gates()
        self.assertEqual((code, self.check(doc, "ci")["status"]), (3, "failed"))

    def test_abbreviated_head_is_not_ready(self):
        self.external_at_head()["body"] = review("example-bot", HEAD[:10])
        self.publish_comments()
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (3, "not_ready"))

    def test_wait_needs_an_author_allowlist(self):
        code, doc = self.run_fg("wait-review", "--head", HEAD, args=["--api-base", self.API["gitea"]])
        self.assertEqual(code, 2)
        self.assertIn("--review-author external=LOGIN", doc["error"]["message"])


class GiteaWait(WaitContract, unittest.TestCase):
    forge = "gitea"


class GitLabGates(GatesContract, unittest.TestCase):
    forge = "gitlab"

    def test_source_pipeline_counts_without_a_target_branch_pipeline(self):
        code, doc = self.gates()
        self.assertEqual((code, self.check(doc, "ci")["runs"][0]["url"]), (0, self.pr["head_pipeline"]["web_url"]))
        self.assertFalse(any("/pipelines" in r["url"] for r in self.fake.requests), "no target-branch pipeline is read")

    def test_pipeline_states(self):
        for state, expected in (("running", (4, "pending")), ("failed", (3, "failed")), ("manual", (4, "pending"))):
            with self.subTest(state):
                self.pr["head_pipeline"]["status"] = state
                self.publish_pr()
                code, doc = self.gates()
                self.assertEqual((code, self.check(doc, "ci")["status"]), expected)

    def test_merged_results_pipeline_is_pending_with_the_reason(self):
        self.pr["head_pipeline"]["sha"] = NEW
        self.publish_pr()
        code, doc = self.gates()
        ci = self.check(doc, "ci")
        self.assertEqual((code, ci["status"]), (4, "pending"))
        self.assertIn("merged-results", ci["detail"])

    def test_no_pipeline(self):
        self.pr["head_pipeline"] = None
        self.publish_pr()
        code, doc = self.gates()
        self.assertEqual((code, self.check(doc, "ci")["status"]), (4, "pending"))

    def test_work_in_progress_on_older_servers(self):
        self.pr.update(draft=None, work_in_progress=True)
        self.publish_pr()
        code, doc = self.gates()
        self.assertEqual((code, self.check(doc, "draft")["draft"]), (3, True))

    def test_abbreviated_head_counts_when_unique(self):
        self.external_at_head()["body"] = review("example-bot", HEAD[:9])
        self.publish_comments()
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (0, "ready"), json.dumps(doc, indent=1))
        twin = HEAD[:9] + "0" * 31
        self.fake.set(f"{self.repo}/merge_requests/42/commits", [{"id": HEAD}, {"id": twin}, {"id": OLD}])
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (3, "not_ready"))


class GitLabWait(WaitContract, unittest.TestCase):
    forge = "gitlab"


class Credentials(Gates, unittest.TestCase):
    forge = "github"
    OVERRIDES = {"GH_TOKEN": GLOBAL_TOKEN, "GITHUB_TOKEN": GLOBAL_TOKEN, "GH_ENTERPRISE_TOKEN": GLOBAL_TOKEN,
                 "GITHUB_ENTERPRISE_TOKEN": GLOBAL_TOKEN, "GH_HOST": "ghe.example.com"}

    def auth(self):
        return {r["headers"].get("Authorization") for r in self.fake.requests}

    def test_anonymous_never_asks_the_cli(self):
        code, doc = self.gates("--anonymous", env=dict(self.OVERRIDES))
        self.assertEqual(code, 0)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.auth(), {None})

    def test_token_file_wins_over_the_cli(self):
        for env in ({"GITHUB_TOKEN_FILE": str(self.token_file)}, {"MY_TOKEN": str(self.token_file)}):
            with self.subTest(env=list(env)):
                self.fake.requests.clear()
                extra = ["--token-env", "MY_TOKEN"] if "MY_TOKEN" in env else []
                code, doc = self.gates(*extra, env=env)
                self.assertEqual((code, self.gh.calls), (0, []))
                self.assertEqual(self.auth(), {f"Bearer {TOKEN}"})

    def test_cli_is_asked_for_the_api_host_without_the_global_overrides(self):
        env = {**self.OVERRIDES, "PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/u/.config/gh"}
        code, doc = self.gates(env=env)
        self.assertEqual(code, 0, json.dumps(doc))
        [(cmd, kw)] = self.gh.calls
        self.assertEqual(cmd, ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertEqual(kw["env"], {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/u/.config/gh"})
        self.assertTrue(kw["timeout"])
        self.assertFalse(kw.get("shell"))
        self.assertEqual(self.auth(), {f"Bearer {CLI_TOKEN}"}, "the synthetic global token never reaches a request")

    def main_with(self, pr, *extra, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = fg.main(["gates", "--pr", pr, *extra], env={} if env is None else env, stdout=out, stderr=err,
                       opener=self.fake, run=self.gh)
        self.assertNotIn(CLI_TOKEN, out.getvalue() + err.getvalue())
        return code, json.loads(out.getvalue())

    def test_enterprise_host_from_the_api_base(self):
        self.fake = FakeApi("https://ghe.example.com/api/v3")
        self.comments = []
        self.build()
        code, doc = self.main_with("https://ghe.example.com/acme/widgets/pull/42")
        self.assertEqual(code, 0, json.dumps(doc))
        self.assertEqual(self.gh.calls[0][0], ["gh", "auth", "token", "--hostname", "ghe.example.com"])
        self.assertTrue(all(r["url"].startswith("https://ghe.example.com/api/v3/") for r in self.fake.requests))

    def test_host_mismatch_reads_nothing(self):
        code, doc = self.gates("--api-base", "https://ghe.example.com/api/v3", env={})
        self.assertEqual(code, 2)
        self.assertIn("different hosts", doc["error"]["message"])
        self.assertEqual((self.gh.calls, self.fake.requests), ([], []))

    def test_api_base_of_another_shape_is_refused_for_the_cli(self):
        code, doc = self.main_with("acme/widgets#42", "--api-base", "https://ghe.example.com/custom")
        self.assertEqual(code, 2)
        self.assertIn("only for https://api.github.com", doc["error"]["message"])
        self.assertEqual((self.gh.calls, self.fake.requests), ([], []))

    def test_owner_repo_form_asks_for_github_com(self):
        code, doc = self.main_with("acme/widgets#42")
        self.assertEqual(code, 0, json.dumps(doc))
        self.assertEqual(self.gh.calls[0][0][-1], "github.com")

    def test_missing_or_logged_out_cli_is_a_named_usage_error(self):
        for gh, why in ((FakeGh(error=FileNotFoundError()), "not installed"), (FakeGh(code=1), "no login for github.com"),
                        (FakeGh(error=subprocess.TimeoutExpired("gh", 15)), "did not answer")):
            with self.subTest(why):
                self.gh = gh
                code, doc = self.gates(env={})
                self.assertEqual(code, 2)
                self.assertIn(why, doc["error"]["message"])
                for choice in ("GITHUB_TOKEN_FILE", "--token-env", "--anonymous"):
                    self.assertIn(choice, doc["error"]["message"])

    def test_gitea_and_gitlab_have_no_cli_rule(self):
        for forge in ("gitea", "gitlab"):
            with self.subTest(forge):
                out, err = io.StringIO(), io.StringIO()
                code = fg.main(["gates", "--pr", self.PR[forge], *self.ARGS[forge]], env=dict(self.OVERRIDES),
                               stdout=out, stderr=err, opener=self.fake, run=self.gh)
                self.assertEqual(code, 2)
                self.assertEqual(self.gh.calls, [])


class MergeCheck(unittest.TestCase):
    """merge-check is verify-delivery's document and exit code."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="forge-gates-"))
        (self.tmp / "token").write_text(TOKEN + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def both(self, forge, edit=None):
        cfg = tvd.FORGES[forge]
        argv = ["--pr", cfg["pr"], *cfg["args"], *cfg.get("authors", [])]
        env = {cfg["env"]: str(self.tmp / "token")}
        results = []
        for main, args in ((vd.main, argv), (fg.main, ["merge-check", *argv])):
            fake = tvd.FakeForge(forge)
            if edit:
                edit(fake)
            out = io.StringIO()
            code = main(args, env=env, stdout=out, stderr=io.StringIO(), opener=fake, sleep=lambda s: None)
            results.append((code, json.loads(out.getvalue())))
        return results

    def test_same_document_as_verify_delivery(self):
        def other_tree(fake):
            merge = fake.data["git_commit_merge"]
            (merge.get("tree") or merge["commit"]["tree"])["sha"] = "1" * 40
        for forge in ("github", "gitea"):
            for name, edit in (("confirmed", None), ("tree differs", other_tree)):
                with self.subTest(forge=forge, case=name):
                    (code, doc), (fg_code, fg_doc) = self.both(forge, edit)
                    self.assertEqual((fg_code, fg_doc), (code, doc))
                    if name == "confirmed":
                        self.assertEqual(code, 0)
                    else:
                        self.assertEqual(next(c for c in doc["checks"] if c["name"] == "tree_equality")["rule"],
                                         "patch_identity")


class MissingSibling(unittest.TestCase):
    def test_a_missing_verify_delivery_is_a_named_usage_error(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="forge-gates-"))
        try:
            scripts = tmp / "skills" / "forge-gates" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy(ROOT / "skills" / "forge-gates" / "scripts" / "forge_gates.py", scripts)
            r = subprocess.run([sys.executable, "-B", "-I", str(scripts / "forge_gates.py"), "gates", "--pr", "acme/widgets#1"],
                               capture_output=True, text=True, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("verify-delivery", json.loads(r.stdout)["error"]["message"])
            self.assertNotIn("Traceback", r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
