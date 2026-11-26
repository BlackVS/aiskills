"""skills/forge-gates/scripts/forge_gates.py against recorded-shape API responses for GitHub,
Gitea and GitLab. No network: a fake opener serves the responses and records every request; no
real clock: the wait's clock and sleep are injected; no real GitHub CLI: a fake records how it
was run.

Run: python3 -m unittest tests/test_forge_gates.py
"""
import base64, datetime, http.client, io, json, socket, pathlib, shutil, subprocess, sys, tempfile, unittest, urllib.error, urllib.parse

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


class Pages(list):
    """A listing served page by page, with a Link rel="next" header (LINK, when given, replaces the
    next page's URL)."""

    def __init__(self, pages, link=None):
        super().__init__(pages)
        self.link = link


class FakeApi:
    """Serves responses by path and query (paging parameters ignored); records each request."""

    def __init__(self, api):
        self.base = urllib.parse.urlsplit(api).path
        self.routes, self.requests = {}, []
        self.posts = {}  # path -> (status, body) answered to a POST or PUT; the written bodies are recorded

    @staticmethod
    def key(path, query):
        return path, frozenset((k, v) for k, v in urllib.parse.parse_qsl(query) if k not in ("per_page", "limit", "page"))

    def set(self, route, body, status=200):
        path, _, query = route.partition("?")
        self.routes[self.key(path, query)] = (status, body)

    def drop(self, route):
        path, _, query = route.partition("?")
        self.routes.pop(self.key(path, query), None)

    def __call__(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": dict(req.header_items()),
                              "timeout": timeout, "data": req.data})
        url = urllib.parse.urlsplit(req.full_url)
        if req.get_method() != "GET":
            post = self.posts.get(url.path[len(self.base):], (404, {"message": "Not Found"}))
            if callable(post):  # an action when the write lands; it returns the answer
                post = post(req)
            if isinstance(post, Exception):
                raise post
            if hasattr(post, "read"):  # a ready answer, such as one whose body cannot be read
                return post
            status, body = post
            data = json.dumps(body).encode()
            if status not in (200, 201):
                raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(data))
            return tvd.Response(status, data)
        status, body = self.routes.get(self.key(url.path[len(self.base):], url.query), (404, {"message": "Not Found"}))
        headers = {}
        if isinstance(body, Pages):
            page = int(dict(urllib.parse.parse_qsl(url.query)).get("page", 1))
            status, body, pages = (*body[page - 1], body)
            if page < len(pages):
                headers["Link"] = f'<{pages.link or req.full_url.split("&page=")[0] + f"&page={page + 1}"}>; rel="next"'
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(data))
        return tvd.Response(200, data, headers)


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
    def run_fg(self, command, *extra, env=None, args=None, pr=None):
        out, err = io.StringIO(), io.StringIO()
        env = dict({self.ENV[self.forge]: str(self.token_file)} if env is None else env)
        if command == "post-review":  # never this machine's own word and host files
            env.setdefault(fg.WORDS_VAR, str(self.tmp / "no-private-words"))
            env.setdefault(fg.HOSTS_VAR, str(self.tmp / "no-private-hosts"))
        argv = [command, "--pr", pr or self.PR[self.forge], *(self.ARGS[self.forge] if args is None else args), *extra]
        code = fg.main(argv, env=env, stdout=out, stderr=err, opener=self.fake, sleep=self.clock.sleep,
                       clock=self.clock, run=self.gh)
        text = self.output = out.getvalue() + err.getvalue()
        for secret in (TOKEN, CLI_TOKEN, GLOBAL_TOKEN):
            self.assertNotIn(secret, text, "no token ever appears in the output")
        self.assertEqual(err.getvalue().count("\n"), 1, "one summary line on stderr")
        posts = [r for r in self.fake.requests if r["method"] != "GET"]
        if command == "post-review":
            self.assertLessEqual(len(posts), 1, "post-review makes at most one write")
            self.assertEqual({r["method"] for r in posts} - {"POST"}, set(), "and never edits")
        elif command == "request-review":
            self.assertLessEqual(len(posts), 1, "request-review makes at most one write")
            self.assertEqual({r["method"] for r in posts} - {"POST", "PUT"}, set(), "and never removes")
        else:
            self.assertEqual(posts, [], "every request is a GET")
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
        self.assertEqual(r["identity_source"], "forge")

    def test_repo_is_not_used_where_the_forge_gives_the_diff(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        code, doc = self.gates("--repo", str(self.tmp / "no-clone-here"))
        r = self.review_of(doc)
        self.assertEqual((code, r["status"], r["identity_source"]), (0, "ready", "forge"), json.dumps(r))

    def test_identity_that_cannot_be_read_is_unknown_not_ready(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        self.set_diff(OLD, b'{"message": "error"}', status=500)
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, doc["status"], r["status"]), (4, "pending", "unknown"), json.dumps(r))
        self.assertFalse(doc["ready"])

    def test_a_base_branch_that_is_not_found_gives_unknown(self):
        self.external_at_head()["body"] = review("example-bot", OLD)
        self.publish_comments()
        self.fake.drop(f"{self.repo}/repository/branches/main" if self.forge == "gitlab" else f"{self.repo}/branches/main")
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, r["status"]), (4, "unknown"), json.dumps(doc))
        self.assertIn("main", r["detail"])

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

    def test_label_events_are_read_only_to_correlate_a_failure(self):
        del self.comments[2]
        self.publish_comments()
        self.wait("--timeout", "120")
        self.assertFalse(any(("/events" in r["url"] or "/timeline" in r["url"] or "label_events" in r["url"])
                             for r in self.fake.requests), "no failure comment: no label events read")
        self.comment("⚠️ The external review could not run.", minutes=-25)  # before the label (-20)
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"], doc["correlation"]), (4, "timeout", "label"))

    def test_a_review_that_uses_the_failure_phrase_is_no_failure(self):
        # the implementer's own review, posted while the external one runs, may say "could not run" in a sentence
        del self.comments[2]
        self.comment(local_review().replace("Add the widget.", "Install where the installer could not run before."),
                     minutes=-5)
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"]), (4, "timeout"), json.dumps(doc))
        # the reviewer's real failure comment, later, still ends the wait
        self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=-4)
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"]), (3, "reviewer_failed"), json.dumps(doc))

    def test_a_conversation_uuid_in_the_failure_is_no_head(self):
        # hands names the conversation; its first group, `e039be33`, is not a SHA of another head
        del self.comments[2]
        self.comment("⚠️ The external review could not run: the review conversation finished without posting "
                     "a review (conversation e039be33-d709-48d9-bd77-fb3fe84e4025); re-add the label.", minutes=-5)
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"]), (3, "reviewer_failed"), json.dumps(doc))

    def test_an_upper_case_uuid_beside_this_head_is_no_other_head(self):
        del self.comments[2]
        self.comment(f"⚠️ The external review could not run for {HEAD[:7]} "
                     "(conversation E039BE33-D709-48D9-BD77-FB3FE84E4025).", minutes=-5)
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"]), (3, "reviewer_failed"), json.dumps(doc))

    def test_a_uuid_does_not_hide_another_head_beside_it(self):
        del self.comments[2]
        self.comment(f"⚠️ The external review could not run for {NEW[:8]} "
                     "(conversation e039be33-d709-48d9-bd77-fb3fe84e4025).", minutes=-5)
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


class RequestContract(Gates):
    """request-review: one existing label is added for the head, only after every refusal was
    decided on the forge's raw answers, then the wait runs as wait-review's."""

    def setUp(self):
        super().setUp()
        del self.comments[2]  # no review of the head yet: the local one only
        self.publish_comments()
        self.pr["labels"] = []
        self.publish_pr()
        self.define_labels("review-this", "review-this:codex-astra", "hands-reviewing", "hands-reviewed")
        self.write_path = (f"{self.repo}/merge_requests/42" if self.forge == "gitlab" else f"{self.repo}/issues/42/labels")
        self.fake.posts[self.write_path] = (200, [] if self.forge != "gitlab" else self.pr)
        # the reviewer answers after the first sleep of the wait
        self.clock.steps = [lambda: self.comment(review("example-bot", HEAD), minutes=3)]

    def define_labels(self, *names, org=()):
        """The labels the forge defines for the repository (and, on Gitea, its organisation)."""
        if self.forge == "github":
            for route in [k for k in self.fake.routes if k[0].startswith(f"{self.repo}/labels/")]:
                del self.fake.routes[route]
            for n in names:
                self.fake.set(f"{self.repo}/labels/{urllib.parse.quote(n, safe='')}", {"id": 900, "name": n})
        elif self.forge == "gitea":
            self.fake.set(f"{self.repo}/labels", [{"id": 70 + i, "name": n} for i, n in enumerate(names)])
            if org:
                self.fake.set("/orgs/acme/labels", [{"id": 90 + i, "name": n} for i, n in enumerate(org)])
        else:
            self.fake.set(f"{self.repo}/labels?include_ancestor_groups=true", [{"id": 70 + i, "name": n}
                                                                                for i, n in enumerate(names + org)])

    def put_labels(self, *names):
        self.pr["labels"] = list(names) if self.forge == "gitlab" else [{"id": 1, "name": n} for n in names]
        self.publish_pr()

    def request(self, *extra, **kw):
        return self.run_fg("request-review", "--head", HEAD, "--interval", "60", "--timeout", "600", *extra, **kw)

    def writes(self):
        return [r for r in self.fake.requests if r["method"] != "GET"]

    def refused(self, doc, name):
        return next(c for c in doc["checks"] if c["name"] == name)["status"] == "failed"

    def test_adds_the_label_then_waits_for_the_review(self):
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"]), (0, "review_found"), json.dumps(doc, indent=1))
        (w,) = self.writes()
        payload = json.loads(w["data"])
        expected = {"github": ("POST", {"labels": ["review-this"]}), "gitea": ("POST", {"labels": [70]}),
                    "gitlab": ("PUT", {"add_labels": "review-this"})}[self.forge]
        self.assertEqual((w["method"], payload), expected)
        self.assertTrue(w["url"].endswith(self.write_path))
        self.assertEqual((doc["request"]["added"], doc["request"]["label"]), (True, "review-this"))
        self.assertTrue(doc["request"]["added_at"])
        self.assertEqual((doc["review"]["verdict"], doc["polls"]), ("READY_FOR_HUMAN_MERGE", 2))
        self.assertEqual(doc["reviews"], {"ready": 1, "required": 2, "summary": "1 of 2 READY"})
        self.assertEqual(doc["reviewed_label"], {"name": "hands-reviewed", "on": False})

    def guide(self, text, status=200):
        """The review guide on the base branch (main), as the forge serves a file's contents."""
        path = ".agents/skills/custom-codereview-guide/SKILL.md"
        route = (f"{self.repo}/repository/files/{urllib.parse.quote(path, safe='')}?ref=main" if self.forge == "gitlab"
                 else f"{self.repo}/contents/{path}?ref=main")
        body = {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"} if status == 200 else {}
        self.fake.set(route, body, status)
        return route

    def test_scope_owners_are_reported_where_the_forge_has_no_owner_role_and_never_refuse(self):
        named = "# Guide\n\nScope owners: alice, planner-bot\n"
        placeholder = "Scope owners: <the accounts, besides a GitHub repository's OWNER, whose\ndisposition comments>\n"
        cases = [("no guide", None, 404, "missing", "no review guide on main"),
                 ("no line", "# Guide\n", 200, "missing", "no Scope owners line"),
                 ("empty line", "Scope owners:\n", 200, "missing", "the Scope owners line is empty"),
                 ("placeholder", placeholder, 200, "missing", "template's placeholder"),
                 ("named", named, 200, "named", None),
                 ("named in a bullet, bold", "- **Scope owners:** alice, planner-bot\n", 200, "named", None),
                 ("unreadable", "x", 500, "unknown", "could not be read")]
        for name, text, status, expected, detail in cases:
            with self.subTest(name):
                self.fake.drop(self.guide("")); self.fake.requests.clear()  # dry runs write nothing
                if text is not None or status != 404:
                    self.guide(text or "", status)
                code, doc = self.request("--dry-run")
                self.assertEqual((code, doc["outcome"]), (0, "dry_run"), "never a refusal: " + json.dumps(doc, indent=1))
                if self.forge == "github":
                    self.assertNotIn("scope_owners", doc)  # OWNER counts there: the guide is not read
                    self.assertFalse(any("custom-codereview-guide" in r["url"] for r in self.fake.requests))
                    continue
                owners = doc["scope_owners"]
                self.assertEqual((owners["status"], owners["base"]), (expected, "main"))
                if expected == "named":
                    self.assertEqual(owners["accounts"], "alice, planner-bot")
                    self.assertNotIn("scope owners", self.stderr_of(doc))
                else:
                    self.assertIn(detail, owners["detail"])
                    if expected == "missing":
                        self.assertIn("Ask the operator which accounts to name", owners["detail"])
                    self.assertIn(f"scope owners {expected}", self.stderr_of(doc))

    def stderr_of(self, doc):
        return fg.summary("request-review", doc)

    def test_a_missing_guide_still_adds_the_label(self):
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"]), (0, "review_found"), json.dumps(doc, indent=1))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(doc.get("scope_owners", {}).get("status"), None if self.forge == "github" else "missing")

    def test_the_write_comes_after_every_read_of_the_refusals(self):
        self.request()
        order = [r["method"] for r in self.fake.requests]
        first_write = order.index("PUT" if self.forge == "gitlab" else "POST")
        reads = [r["url"] for r in self.fake.requests[:first_write]]
        self.assertTrue(any("label" in u for u in reads), "the label is looked up before the write")
        self.assertTrue(any(("/notes" in u or "/comments" in u) for u in reads), "the comments are read before it")

    def test_dry_run_reads_and_refuses_but_writes_nothing(self):
        code, doc = self.request("--dry-run")
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (0, "dry_run", False))
        self.assertEqual(self.writes(), [])
        self.assertIn("review-this would be added", self.output)
        self.assertEqual(self.clock.sleeps, [], "no wait")
        self.put_labels("hands-reviewing")
        code, doc = self.request("--dry-run")
        self.assertEqual((code, doc["outcome"]), (3, "refused"))

    def test_anonymous_only_with_dry_run(self):
        code, doc = self.request("--anonymous", env={})
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
        self.assertEqual(self.fake.requests, [], "refused before any read")
        code, doc = self.request("--anonymous", "--dry-run", env={})
        self.assertEqual((code, doc["outcome"]), (0, "dry_run"))

    def assert_refused(self, check, *extra):
        code, doc = self.request(*extra)
        self.assertEqual((code, doc["outcome"]), (3, "refused"), json.dumps(doc, indent=1))
        self.assertTrue(self.refused(doc, check), json.dumps(doc["checks"], indent=1))
        self.assertEqual(self.writes(), [], "a refusal writes nothing")
        self.assertEqual(self.clock.sleeps, [], "and does not wait")
        return doc

    def test_refuses_a_moved_head(self):
        if self.forge == "gitlab":
            self.pr["sha"] = NEW
        else:
            self.pr["head"]["sha"] = NEW
        self.publish_pr()
        self.assertIn(NEW, next(c for c in self.assert_refused("head")["checks"] if c["name"] == "head")["detail"])

    def test_refuses_a_closed_or_merged_pull_request(self):
        for state in ("closed", "merged", "locked"):
            if state == "locked" and self.forge != "gitlab":
                continue
            with self.subTest(state):
                self.fake.requests.clear()
                if self.forge == "gitlab":
                    self.pr["state"] = state
                else:
                    self.pr.update(state="closed", merged=state == "merged")
                self.publish_pr()
                self.assert_refused("state")

    def test_refuses_while_a_review_runs(self):
        self.put_labels("hands-reviewing")
        self.assert_refused("in_progress_label")

    def test_refuses_a_request_label_already_on_plain_or_variant(self):
        for on in ("review-this", "review-this:claude-fable"):
            with self.subTest(on):
                self.fake.requests.clear()
                self.put_labels(on)
                self.assert_refused("request_label")
                self.assert_refused("request_label", "--variant", "codex-astra")

    def test_refuses_when_a_review_of_the_head_exists(self):
        self.comment(review("example-bot", HEAD), minutes=-10)
        self.assert_refused("head_review")

    def test_a_newer_failure_lets_the_review_be_requested_again_and_the_old_review_is_no_answer(self):
        old = self.comment(review("example-bot", HEAD), minutes=-10)
        self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=-5)
        self.labels.append(self.labels[0] | {"created_at": stamp(0)})  # the label event of this request
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"]), (0, "review_found"), json.dumps(doc, indent=1))
        self.assertEqual(len(self.writes()), 1)
        self.assertNotEqual(doc["review"]["url"], old.get("html_url") or "", "the answer is the new review")
        self.assertEqual(doc["polls"], 2, "the review from before the request did not end the wait")

    def test_a_failure_older_than_the_review_does_not_let_it_be_requested_again(self):
        self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=-15)
        self.comment(review("example-bot", HEAD), minutes=-10)
        self.assert_refused("head_review")

    def test_a_404_while_reading_refuses_naming_the_read(self):
        self.fake.drop(f"{self.repo}/merge_requests/42/notes?sort=asc&order_by=created_at" if self.forge == "gitlab"
                       else f"{self.repo}/issues/42/comments")
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["reason"]), (3, "refused", "the forge answered 404 while reading the comments"))
        self.assertEqual(self.writes(), [])

    def test_refuses_a_label_the_forge_does_not_define_and_never_creates_one(self):
        self.define_labels("hands-reviewing")
        doc = self.assert_refused("label")
        self.assertIn("never creates", next(c for c in doc["checks"] if c["name"] == "label")["detail"])

    def test_a_variant_is_added_only_when_it_exists(self):
        code, doc = self.request("--variant", "codex-astra")
        self.assertEqual((code, doc["request"]["label"]), (0, "review-this:codex-astra"))
        payload = json.loads(self.writes()[0]["data"])
        self.assertIn(payload, ({"labels": ["review-this:codex-astra"]}, {"labels": [71]},
                                {"add_labels": "review-this:codex-astra"}))
        self.fake.requests.clear()
        self.clock.steps, self.clock.sleeps = [], []
        self.assert_refused("label", "--variant", "cc-sol")  # no fallback to the plain label

    def test_a_variant_must_be_a_profile_name(self):
        code, doc = self.request("--variant", "a b")
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
        self.assertEqual(self.fake.requests, [])

    def test_the_stale_reviewed_label_is_reported_and_left_on(self):
        self.put_labels("hands-reviewed")
        code, doc = self.request()
        self.assertEqual((code, doc["reviewed_label"]["on"]), (0, True))
        self.assertIn("remove it by hand", doc["reviewed_label"]["detail"])
        self.assertIn("hands-reviewed is on", self.output)
        self.assertEqual(len(self.writes()), 1, "nothing is removed")

    def test_a_refused_write_says_no_label_was_added(self):
        for status, code in ((403, 2), (422, 4), (500, 4)):
            with self.subTest(status):
                self.fake.requests.clear()
                self.fake.posts[self.write_path] = (status, {"message": "no"})
                got, doc = self.request()
                self.assertEqual((got, doc["outcome"], doc["request"]["added"]), (code, "write_failed", False))
                self.assertEqual(len(self.writes()), 1, "never retried")

    def test_a_lost_answer_says_the_label_may_be_on(self):
        self.fake.posts[self.write_path] = urllib.error.URLError("connection reset")
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (4, "write_unknown", "unknown"))
        self.assertIn("read the pull request's labels", doc["request"]["detail"])
        self.assertEqual((len(self.writes()), self.clock.sleeps), (1, []))

    def test_a_wait_that_fails_after_the_write_keeps_the_request(self):
        self.clock.steps = []
        code, doc = self.request("--timeout", "120")
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (4, "timeout", True))
        self.fake.requests.clear()
        self.clock.steps = [lambda: self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=4)]
        self.labels.append(self.labels[0] | {"created_at": stamp(1)})  # this request's label
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (3, "reviewer_failed", True))

    def test_a_head_pushed_during_the_wait_keeps_the_request(self):
        self.fake.set(f"{self.repo}/merge_requests/42" if self.forge == "gitlab" else f"{self.repo}/pulls/42",
                      json.loads(json.dumps(self.pr)))  # the head the reads before the write see
        if self.forge == "gitlab":
            self.pr["sha"] = NEW
        else:
            self.pr["head"]["sha"] = NEW
        self.clock.steps = [self.publish_pr]  # pushed while the reviewer works
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (3, "head_moved", True))

    def test_an_error_while_waiting_keeps_the_request(self):
        original = fg.wait_review

        def broken(*a, **kw):
            raise KeyError("x")
        fg.wait_review = broken
        try:
            code, doc = self.request()
        finally:
            fg.wait_review = original
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (3, "error", True))
        self.assertIn("KeyError while waiting", doc["error"]["message"])

    def test_an_outage_before_the_write_is_pending_and_writes_nothing(self):
        self.fake.set(f"{self.repo}/merge_requests/42" if self.forge == "gitlab" else f"{self.repo}/pulls/42",
                      {"message": "down"}, status=503)
        code, doc = self.request()
        self.assertEqual((code, doc["error"]["kind"]), (4, "unavailable"))
        self.assertEqual(self.writes(), [])

    def test_malformed_answers_refuse_naming_the_read_and_write_nothing(self):
        head_key = "sha" if self.forge == "gitlab" else "head"
        # a value of the wrong type refuses, either in its own check or as a surprise in the read itself
        cases = [("labels", 7, ("the pull request's labels is not a list",)),
                 ("labels", [{"id": 1}] if self.forge != "gitlab" else [None], ("without a name",)),
                 ("labels", ["review-this"] if self.forge != "gitlab" else [{"name": "x"}], ("without a name",)),
                 ("labels", [{"name": "\ud800"}] if self.forge != "gitlab" else ["\ud800"], ("without a name",)),
                 (head_key, 42, ("head", "while reading the pull request")),
                 ("state", ["open"], ("state", "while reading the pull request")),
                 ("state", None, ("state",))]
        for field, value, expect in cases:
            with self.subTest(field=field, value=value):
                self.build()
                del self.comments[2]
                self.publish_comments()
                self.pr["labels"] = []
                self.pr[field] = value
                self.publish_pr()
                self.fake.requests.clear()
                code, doc = self.request()
                self.assertEqual((code, doc["outcome"]), (3, "refused"), json.dumps(doc, indent=1))
                said = doc.get("reason") or json.dumps([c for c in doc["checks"] if c["status"] == "failed"])
                self.assertTrue(any(e in said for e in expect), said)
                self.assertEqual(self.writes(), [])

    def test_a_surprise_in_any_read_refuses_naming_it(self):
        for target, step in (("read_pull", "reading the pull request"), ("trusted_comments", "reading the comments"),
                             ("forge_label", "reading the forge's labels")):
            with self.subTest(target):
                original = getattr(fg, target)
                setattr(fg, target, lambda *a, **kw: (_ for _ in ()).throw(TypeError("x")))
                self.fake.requests.clear()
                try:
                    code, doc = self.request()
                finally:
                    setattr(fg, target, original)
                self.assertEqual((code, doc["reason"]), (3, f"TypeError while {step}"))
                self.assertEqual(self.writes(), [])

    def test_a_label_listing_that_is_not_a_list_refuses(self):
        if self.forge == "github":
            self.fake.set(f"{self.repo}/labels/review-this", ["review-this"])
        elif self.forge == "gitea":
            self.fake.set(f"{self.repo}/labels", {"labels": []})
        else:
            self.fake.set(f"{self.repo}/labels?include_ancestor_groups=true", [["review-this"]])
        code, doc = self.request()
        self.assertEqual((code, self.refused(doc, "label")), (3, True), json.dumps(doc, indent=1))
        self.assertEqual(self.writes(), [])


class BrokenBody(tvd.Response):
    """A 200 whose body breaks off: the status arrived, the rest of the answer did not."""

    def read(self, n=-1):
        raise http.client.IncompleteRead(b"[")


class RequestRoundTwo:
    """The external review's return at 1f0b6e1: the preflight reads strictly, the write takes its
    status alone, a resumed wait keeps the cutoff, and a 404 in the wait maps as wait-review's."""

    def comments_route(self):
        return (f"{self.repo}/merge_requests/42/notes?sort=asc&order_by=created_at" if self.forge == "gitlab"
                else f"{self.repo}/issues/42/comments")

    def test_a_comments_page_that_is_not_a_list_refuses_and_writes_nothing(self):
        for page in ({}, {"comments": []}, [["not an object"]], "text"):
            with self.subTest(page=page):
                self.fake.requests.clear()
                self.fake.set(self.comments_route(), page)
                code, doc = self.request()
                self.assertEqual((code, doc["outcome"]), (3, "refused"), json.dumps(doc, indent=1))
                self.assertIn("while reading the comments", doc["reason"])
                self.assertEqual(self.writes(), [])

    def test_each_decision_field_of_a_comment_of_another_type_refuses_and_writes_nothing(self):
        """Every field of a comment that reaches a decision, wrong in turn: the comment refuses the
        request before any filter or trust rule, and is never dropped."""
        cases = [("body", "body", None), ("body", "body", 42),
                 ("author", "author" if self.forge == "gitlab" else "user", None),
                 ("author", "author" if self.forge == "gitlab" else "user", {"username": 42, "login": 42}),
                 ("created_at", "created_at", None), ("created_at", "created_at", "yesterday"),
                 ("created_at", "created_at", "2026-09-27T12:00:00"), ("created_at", "created_at", 1732445194)]
        if self.forge == "github":
            cases += [("author_association", "author_association", None), ("author_association", "author_association", 7)]
        if self.forge == "gitlab":
            cases += [("system", "system", "false"), ("system", "system", None), ("system", "system", 0)]
        for name, key, value in cases:
            with self.subTest(field=key, value=value):
                self.fake.requests.clear()
                c = dict(self.comments[1])  # the local review: a comment every rule reads
                c[key] = value
                self.fake.set(self.comments_route(), [self.comments[0], c] + self.comments[2:])
                code, doc = self.request()
                self.assertEqual((code, doc["outcome"]), (3, "refused"), json.dumps(doc, indent=1))
                self.assertIn(f"a comment's {name} is not of the type the forge documents while reading the comments",
                              doc["reason"])
                self.assertEqual(self.writes(), [])

    def test_wait_review_still_reads_the_comments_as_before(self):
        self.fake.set(self.comments_route(), {})
        self.clock.steps = []
        code, doc = self.wait("--timeout", "60")
        self.assertEqual((code, doc["outcome"]), (4, "timeout"), "the standalone wait is unchanged")

    def test_a_200_whose_body_breaks_off_counts_as_added(self):
        self.fake.posts[self.write_path] = BrokenBody(200, b"")
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (0, "review_found", True), json.dumps(doc))
        self.assertEqual(len(self.writes()), 1)

    def test_a_status_that_never_arrives_is_unknown_and_never_retried(self):
        for error in (urllib.error.URLError("reset"), socket.timeout("timed out"), TimeoutError(),
                      ConnectionResetError(), http.client.RemoteDisconnected("closed"), http.client.BadStatusLine("")):
            with self.subTest(error=type(error).__name__):
                self.fake.requests.clear()
                self.fake.posts[self.write_path] = error
                code, doc = self.request()
                self.assertEqual((code, doc["outcome"], doc["request"]["added"]), (4, "write_unknown", "unknown"))
                self.assertEqual(len(self.writes()), 1, "never retried")
                self.assertEqual(self.clock.sleeps, [], "and no wait")

    def re_request(self):
        """An earlier READY review of the head, then the reviewer's failure for it; this request's label."""
        old = self.comment(review("example-bot", HEAD), minutes=-10)
        failure = self.comment(f"⚠️ The external review could not run for {HEAD[:7]}.", minutes=-5)
        self.labels.append(self.labels[0] | {"created_at": stamp(0)})
        return old, failure

    def test_a_resumed_wait_keeps_the_cutoff_the_forge_gave(self):
        old, failure = self.re_request()
        self.clock.steps = []
        url = self.PR[self.forge] + "?a=1&b=two words"  # a reference a shell string would split
        code, doc = self.run_fg("request-review", "--head", HEAD, "--interval", "60", "--timeout", "120", pr=url)
        self.assertEqual((code, doc["outcome"]), (4, "timeout"), json.dumps(doc, indent=1))
        resume = doc["request"]["resume"]
        self.assertEqual(resume, ["wait-review", "--pr", url, "--head", HEAD, "--review-after", failure["created_at"]],
                         "an argument list: the reference, the head and the failure's time as the forge gave it")
        # the second invocation: the arguments as the document gives them
        self.fake.requests.clear()
        code, doc = self.run_fg(resume[0], *resume[3:], "--interval", "60", "--timeout", "120", pr=resume[2])
        self.assertEqual((code, doc["outcome"]), (4, "timeout"), "the superseded review is no answer")
        self.clock.steps = [lambda: self.comment(review("example-bot", HEAD), minutes=30)]
        self.fake.requests.clear()
        code, doc = self.run_fg(resume[0], *resume[3:], "--interval", "60", "--timeout", "120", pr=resume[2])
        self.assertEqual((code, doc["outcome"]), (0, "review_found"))
        self.assertNotEqual(doc["review"]["url"], self.PR[self.forge] + self.anchor(old))

    def anchor(self, c):
        return f"#note_{c['id']}" if self.forge == "gitlab" else f"#issuecomment-{c['id']}"

    def test_wait_review_without_the_option_is_unchanged(self):
        old, _ = self.re_request()
        code, doc = self.wait()
        self.assertEqual((code, doc["outcome"], doc["review"]["url"]),
                         (0, "review_found", self.PR[self.forge] + self.anchor(old)))
        self.assertEqual(doc["polls"], 1)

    def test_a_review_after_that_is_not_a_time_is_a_usage_error(self):
        for value in ("yesterday", "2026-11-24T10:46:34", "", "1732445194"):
            with self.subTest(value=value):
                self.fake.requests.clear()
                code, doc = self.wait("--review-after", value)
                self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
                self.assertIn("--review-after", doc["error"]["message"])
                self.assertEqual(self.fake.requests, [], "nothing is read")

    def test_a_404_in_the_wait_maps_as_wait_reviews_and_keeps_the_request(self):
        self.fake.drop(self.comments_route())
        standalone = self.wait()
        self.publish_comments()

        def land(req):
            self.fake.drop(self.comments_route())  # gone once the label is on
            return 200, []
        self.fake.posts[self.write_path] = land
        code, doc = self.request()
        self.assertEqual((code, doc["outcome"], doc["error"]["kind"]), (standalone[0], "error", standalone[1]["error"]["kind"]))
        self.assertEqual((code, doc["error"]["kind"]), (3, "not_found"))
        self.assertEqual(doc["request"]["added"], True)


def local_review(sha=HEAD, header="[oh-code-review (high) review]", verdict="VERDICT\nREADY_FOR_HUMAN_MERGE",
                 attribution="Review: oh-code-review (high) via Claude Code"):
    return (f"{header} reviewed at head {sha}\n\nCURRENT OBJECTIVE\nAdd the widget.\n\nBLOCKERS\n- None\n\n"
            f"{verdict}\nNo blockers.\n\nRISK\nLOW\n\n{attribution}\n")


class PostContract(Gates):
    """post-review: a review is posted only in the form the delivery check reads, for the head."""

    def setUp(self):
        super().setUp()
        self.posted_url = self.PR[self.forge] + ("#note_99" if self.forge == "gitlab" else "#issuecomment-99")
        answer = {"id": 99} if self.forge == "gitlab" else {"id": 99, "html_url": self.posted_url}
        self.comments_path = (f"{self.repo}/merge_requests/42/notes" if self.forge == "gitlab"
                              else f"{self.repo}/issues/42/comments")
        self.fake.posts[self.comments_path] = (201, answer)

    def post(self, body, *extra, **kw):
        f = self.tmp / "review.md"
        f.write_text(body, encoding="utf-8")
        return self.run_fg("post-review", "--body-file", str(f), *extra, **kw)

    def posts(self):
        return [r for r in self.fake.requests if r["method"] == "POST"]

    @staticmethod
    def failed(doc):
        return {c["name"]: c.get("line") for c in doc["checks"] if c["status"] == "failed"}

    def test_a_well_formed_review_of_the_head_is_posted_once_as_it_is(self):
        body = local_review()
        code, doc = self.post(body)
        self.assertEqual((code, doc["outcome"]), (0, "posted"), json.dumps(doc, indent=1))
        self.assertEqual(doc["comment"]["url"], self.posted_url)
        [req] = self.posts()
        self.assertTrue(req["url"].endswith(self.comments_path))
        self.assertEqual(json.loads(req["data"]), {"body": body})
        self.assertIn(TOKEN, req["headers"]["Authorization"])

    def test_a_long_review_still_gives_the_comment_url(self):
        body = local_review().replace("Add the widget.", "Add the widget. " + "ї" * 40000, 1)  # 80 kB of UTF-8 in 40000 characters
        answer = dict(self.fake.posts[self.comments_path][1], body=body)  # the forge echoes the body back
        self.fake.posts[self.comments_path] = (201, answer)
        code, doc = self.post(body)
        self.assertEqual((code, doc["comment"]["url"]), (0, self.posted_url))
        self.assertEqual(self.post(body + "y" * 26000)[1]["checks"][-3], {"name": "size", "status": "failed",
                                                                          "detail": "longer than 65536 characters"})

    def test_what_it_posts_is_what_the_gates_count(self):
        self.comments[1:2] = []  # no local review yet
        self.publish_comments()
        self.assertNotEqual(self.review_of(self.gates()[1], "local")["status"], "ready")
        body = local_review()
        self.assertEqual(self.post(body)[0], 0)
        self.fake.requests.clear()
        self.comment(body, author="author", minutes=-1, association="MEMBER")
        self.assertEqual(self.review_of(self.gates()[1], "local")["status"], "ready")

    def test_every_header_form_the_delivery_check_reads_is_posted_and_counted(self):
        self.comments[1:2] = []  # no local review yet
        for header in ("[oh-code-review high]", "[oh-code-review review]"):  # the level-less form names no level
            with self.subTest(header=header):
                body = local_review(header=header)
                code, doc = self.post(body, "--dry-run")
                self.assertEqual((code, doc["outcome"]), (0, "dry_run"), json.dumps(doc, indent=1))
                self.assertEqual(self.post(body)[0], 0)
                self.fake.requests.clear()
                self.comments[1:] = []
                self.comment(body, author="author", minutes=-1, association="MEMBER")
                self.assertEqual(self.review_of(self.gates()[1], "local")["status"], "ready")

    def test_dry_run_checks_everything_and_posts_nothing(self):
        code, doc = self.post(local_review(), "--dry-run")
        self.assertEqual((code, doc["outcome"]), (0, "dry_run"))
        self.assertEqual(self.posts(), [])
        code, doc = self.post(local_review(sha=OLD), "--dry-run")
        self.assertEqual((code, doc["outcome"], self.failed(doc)), (3, "refused", {"head": None}))

    def test_the_format_misses_are_refused_with_their_line(self):
        cases = {
            "abbreviated head": (local_review(sha=HEAD[:7]), {"header": 1, "head": None}),
            "markdown header": (local_review().replace("[oh-code-review (high) review] reviewed at head",
                                                       "## Review: oh-code-review (high) via Claude Code, head", 1),
                                {"header": 1, "head": None}),
            "a level the delivery check does not read": (local_review(header="[oh-code-review (medium) review]",
                                                                      attribution="Review: oh-code-review (medium) via Claude Code"),
                                                         {"header": 1, "head": None}),
            "one-line verdict": (local_review(verdict="VERDICT: READY_FOR_HUMAN_MERGE (the observation does not block)."),
                                 {"verdict": 9}),
            "bold verdict": (local_review(verdict="**VERDICT:** `READY_FOR_HUMAN_MERGE`"), {"verdict": 9}),
            "prose on the verdict line": (local_review(verdict="VERDICT\nREADY_FOR_HUMAN_MERGE (no blockers)"),
                                          {"verdict": 10}),
            "two verdicts": (local_review(verdict="VERDICT\nREADY_FOR_HUMAN_MERGE\n\nVERDICT\nREADY_FOR_HUMAN_MERGE"),
                             {"verdict": 12}),
            "no verdict": (local_review(verdict=""), {"verdict": None}),
            "unknown verdict word": (local_review(verdict="VERDICT\nLGTM"), {"verdict": 10}),
            "no attribution": (local_review(attribution=""), {"attribution": None}),
            "attribution of another level": (local_review(attribution="Review: oh-code-review (max) via Claude Code"),
                                             {"attribution": 16}),
            "session link": (local_review() + "https://claude.ai/code/session_0123456789\n", {"session_links": 17}),
            "session trailer": (local_review() + "Claude-Session: 0123456789\n", {"session_links": 17}),
            "older head": (local_review(sha=OLD), {"head": None}),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(case=name):
                self.fake.requests.clear()
                code, doc = self.post(body)
                self.assertEqual((code, doc["outcome"]), (3, "refused"), json.dumps(doc, indent=1))
                self.assertEqual(self.failed(doc), expected)
                self.assertEqual(self.posts(), [])

    def test_a_review_after_the_merge_is_refused(self):
        if self.forge == "gitlab":
            self.pr["state"] = "merged"
        else:
            self.pr.update(state="closed", merged=True)
        self.publish_pr()
        code, doc = self.post(local_review())
        self.assertEqual((code, self.failed(doc)), (3, {"state": None}))
        self.assertIn("ignored by the delivery check", self.check(doc, "state")["detail"])
        self.assertEqual(self.posts(), [])

    def test_deny_file_entries_are_refused_and_never_printed(self):
        deny = self.tmp / "deny"
        deny.write_text("# private names\n\nInternal-Hub.example\n", encoding="utf-8")
        code, doc = self.post(local_review() + "Seen on internal-hub.example.\n", "--deny-file", str(deny))
        self.assertEqual((code, self.failed(doc)), (3, {"deny_file": 17}))
        self.assertNotIn("internal-hub", json.dumps(doc).lower())
        self.assertEqual(self.post(local_review(), "--deny-file", str(deny))[0], 0)

    def test_deny_file_entries_match_as_substrings_as_released(self):
        """Unlike the word file, a deny entry matches inside a longer word: a token prefix, a host fragment."""
        deny = self.tmp / "deny"
        deny.write_text("tok_\nnternal-hu\n", encoding="utf-8")
        for text in ("A token tok_abcDEF123 leaked.\n", "Seen on internal-hub.example.\n"):
            with self.subTest(text=text):
                code, doc = self.post(local_review() + text, "--deny-file", str(deny))
                self.assertEqual((code, self.failed(doc)), (3, {"deny_file": 17}))

    def private_words_env(self, hosts=""):
        words, hosts_file = self.tmp / "private-words", self.tmp / "private-hosts"
        words.write_text("Zorbex\nvexhub\n!hands.vexhub.example\n", encoding="utf-8")
        hosts_file.write_text(hosts, encoding="utf-8")
        return {self.ENV[self.forge]: str(self.token_file), fg.WORDS_VAR: str(words), fg.HOSTS_VAR: str(hosts_file)}

    def test_the_private_word_file_applies_by_default_and_never_shows_a_word_or_its_line(self):
        env = self.private_words_env()
        line = "the line that must never be shown"
        code, doc = self.post(local_review() + f"Deployed on group-zorbex, {line}.\n", env=env)
        self.assertEqual((code, self.failed(doc)), (3, {"private_words": 17}))
        for text in ("zorbex", line):
            self.assertNotIn(text, self.output.lower())
        self.assertEqual(self.posts(), [])
        # The allowed literal passes; the same word outside it does not.
        self.assertEqual(self.post(local_review() + "Review: https://hands.vexhub.example/run/1\n", env=env)[0], 0)
        self.assertEqual(self.post(local_review() + "Seen on vexhub.\n", env=dict(env))[0], 3)

    def test_a_forge_on_a_private_host_does_not_use_the_word_file(self):
        host = fg.remote_host(self.API[self.forge])
        env = self.private_words_env(hosts=host.split(".", 1)[-1] + "\n")
        code, doc = self.post(local_review() + "Deployed on group-zorbex.\n", env=env)
        self.assertEqual(code, 0, json.dumps(doc, indent=1))
        [note] = [c for c in doc["checks"] if c["name"] == "private_words"]
        self.assertIn("listed as private", note["note"])
        # An explicit --deny-file still applies there.
        deny = self.tmp / "deny"
        deny.write_text("zorbex\n", encoding="utf-8")
        self.assertEqual(self.failed(self.post(local_review() + "Deployed on group-zorbex.\n",
                                               "--deny-file", str(deny), env=env)[1]), {"deny_file": 17})

    def test_forge_refusals(self):
        for answer, expected in ((403, 2), (500, 4)):
            with self.subTest(status=answer):
                self.fake.requests.clear()
                self.fake.posts[self.comments_path] = (answer, {"message": "no"})
                code, doc = self.post(local_review())
                self.assertEqual((code, doc["outcome"]), (expected, "error"))
        self.fake.posts[self.comments_path] = urllib.error.URLError("reset")
        self.fake.requests.clear()
        code, doc = self.post(local_review())
        self.assertEqual(code, 4)
        self.assertIn("may or may not have been posted", doc["error"]["message"])
        self.assertEqual(len(self.posts()), 1, "a write is never retried")


class GitHubGates(GatesContract, unittest.TestCase):
    forge = "github"

    def test_a_compare_the_forge_cannot_serve_falls_back_to_git_with_repo(self):
        # a 404 from the compare (a head no longer on the forge, for example): with --repo, git
        # computes both identities; the full SHAs name the same commits in the clone
        self.clone, shas = tvd.real_repo(self.tmp)
        r = self.repo
        self.pr["head"]["sha"] = shas["H"]
        self.publish_pr()
        self.fake.set(f"{r}/branches/main", {"name": "main", "commit": {"sha": shas["T"]}})
        self.fake.set(f"{r}/commits/{shas['H']}/check-runs?filter=latest", {"check_runs": self.runs})
        self.fake.set(f"{r}/commits/{shas['H']}/status", {"state": "success", "statuses": self.statuses})
        self.comments[:] = []
        self.comment(review("local", shas["H"]), author="author", minutes=-60, association="MEMBER")
        self.comment(review("example-bot", shas["O"]), minutes=-10)
        code, doc = self.gates()
        self.assertEqual((code, self.review_of(doc)["status"]), (4, "unknown"), "no compare, no --repo: unknown")
        code, doc = self.gates("--repo", str(self.clone))
        rv = self.review_of(doc)
        self.assertEqual((code, rv["status"], rv.get("carried_from"), rv["identity_source"]),
                         (0, "ready", shas["O"], "git"), json.dumps(doc, indent=1))

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

    def test_every_status_page_is_read(self):
        ok = [{"context": f"ci/{i}", "id": i, "target_url": "", "state": "success"} for i in range(100)]
        route = f"{self.repo}/commits/{HEAD}/status"
        for last, expected in (("success", (0, "passed")), ("failure", (3, "failed")), ("pending", (4, "pending"))):
            with self.subTest(last):
                tail = [{"context": "ci/late", "id": 100, "target_url": "", "state": last}]
                self.fake.set(route, Pages([(200, {"state": last, "statuses": ok}), (200, {"state": last, "statuses": tail})]))
                code, doc = self.gates()
                ci = self.check(doc, "ci")
                self.assertEqual((code, ci["status"]), expected)
                self.assertEqual(len(ci["runs"]), 102, "one check run and 101 statuses")
        with self.subTest("second page unavailable"):
            self.fake.set(route, Pages([(200, {"state": "failure", "statuses": ok}), (500, {"message": "error"})]))
            code, doc = self.gates()
            self.assertEqual((code, doc["status"], doc["error"]["kind"]), (4, "pending", "unavailable"))
        with self.subTest("next page on another host"):
            self.fake.set(route, Pages([(200, {"state": "failure", "statuses": ok}), (200, {"statuses": []})],
                                       link="https://elsewhere.example.org/next"))
            code, doc = self.gates()
            self.assertEqual((code, doc["ready"]), (2, False))
            self.assertFalse(any("elsewhere" in r["url"] for r in self.fake.requests))

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


class GitHubPost(PostContract, unittest.TestCase):
    forge = "github"

    def test_anonymous_only_checks(self):
        self.assertEqual(self.post(local_review(), "--anonymous", "--dry-run", env={})[0], 0)
        code, doc = self.post(local_review(), "--anonymous", env={})
        self.assertEqual((code, doc["error"]["kind"]), (2, "usage"))
        self.assertEqual(self.posts(), [])


class GitHubWait(WaitContract, unittest.TestCase):
    forge = "github"


class GiteaGates(GatesContract, unittest.TestCase):
    forge = "gitea"

    # ---- before 1.27 the compare API answers JSON: with --repo, git computes both identities
    def on_real_commits(self, head="H"):
        """The pull request moved onto real commits (tvd.real_repo): the base tip T, the head HEAD
        (H, a base-only update of O, or S, a different change), the local review at the head and
        the external review READY at O only. The compare API answers JSON."""
        self.clone, shas = tvd.real_repo(self.tmp)
        r, current = self.repo, shas[head]
        self.pr["head"]["sha"] = current
        self.publish_pr()
        self.fake.set(f"{r}/branches/main", {"name": "main", "commit": {"id": shas["T"]}})
        self.fake.set(f"{r}/commits/{current}/status", {"state": "success", "statuses": self.statuses})
        for sha in (shas["O"], current):
            self.fake.set(f"{r}/compare/{shas['T']}...{sha}?output=diff", json.dumps({"commits": []}).encode())
        self.comments[:] = []
        self.comment(review("local", current), author="author", minutes=-60)
        self.comment(review("example-bot", shas["O"]), minutes=-10)
        return shas

    def test_an_older_gitea_carries_a_review_over_with_repo(self):
        shas = self.on_real_commits()
        code, doc = self.gates("--repo", str(self.clone))
        r = self.review_of(doc)
        self.assertEqual((code, doc["status"], r["status"], r.get("carried_from"), r["identity_source"]),
                         (0, "ready", "ready", shas["O"], "git"), json.dumps(doc, indent=1))
        self.assertEqual(r["change_identity"]["reviewed"], r["change_identity"]["current"])
        self.assertEqual(self.check(doc, "reviews")["base_tip"], shas["T"])

    def test_without_repo_an_older_gitea_names_the_option(self):
        self.on_real_commits()
        code, doc = self.gates()
        r = self.review_of(doc)
        self.assertEqual((code, r["status"]), (4, "unknown"))
        self.assertIn("1.27", r["detail"])
        self.assertIn("--repo", r["detail"])

    def test_git_does_not_carry_a_review_over_a_different_change(self):
        shas = self.on_real_commits(head="S")
        code, doc = self.gates("--repo", str(self.clone))
        r = self.review_of(doc)
        self.assertEqual((code, r["status"], r["identity_source"]), (3, "stale", "git"), json.dumps(r))
        self.assertNotEqual(r["change_identity"]["reviewed"], r["change_identity"]["current"])

    def test_a_clone_without_the_commits_gives_unknown(self):
        self.on_real_commits()
        empty = self.tmp / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q", str(empty)], env=tvd.git_env(self.tmp, helper=False), check=True)
        code, doc = self.gates("--repo", str(empty))
        r = self.review_of(doc)
        self.assertEqual((code, r["status"], r["identity_source"]), (4, "unknown", "git"), json.dumps(r))
        self.assertIn("fetch it", r["detail"])

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


class GiteaPost(PostContract, unittest.TestCase):
    forge = "gitea"


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


class GitLabPost(PostContract, unittest.TestCase):
    forge = "gitlab"


class GitLabWait(WaitContract, unittest.TestCase):
    forge = "gitlab"


class GitHubRequest(RequestRoundTwo, RequestContract, unittest.TestCase):
    forge = "github"

    def test_a_label_found_under_another_case_is_not_this_label(self):
        self.fake.set(f"{self.repo}/labels/review-this", {"id": 900, "name": "Review-This"})
        code, doc = self.request()
        self.assertEqual((code, self.refused(doc, "label")), (3, True))
        self.assertEqual(self.writes(), [])


class GiteaRequest(RequestRoundTwo, RequestContract, unittest.TestCase):
    forge = "gitea"

    def test_an_organisation_label_is_written_by_its_id(self):
        self.define_labels("hands-reviewing", org=("review-this",))
        code, doc = self.request()
        self.assertEqual(code, 0, json.dumps(doc, indent=1))
        self.assertEqual(json.loads(self.writes()[0]["data"]), {"labels": [90]})

    def test_a_label_without_a_numeric_id_refuses(self):
        self.fake.set(f"{self.repo}/labels", [{"id": "70", "name": "review-this"}])
        code, doc = self.request()
        self.assertEqual((code, self.refused(doc, "label")), (3, True))
        self.assertEqual(self.writes(), [])


class GitLabRequest(RequestRoundTwo, RequestContract, unittest.TestCase):
    forge = "gitlab"

    def test_a_label_missing_from_the_project_and_its_groups_makes_no_request(self):
        # add_labels would create it: the read before the write is what keeps the command from doing so
        self.define_labels("hands-reviewing", "hands-reviewed")
        code, doc = self.request()
        self.assertEqual((code, self.refused(doc, "label")), (3, True))
        self.assertEqual([r for r in self.fake.requests if r["method"] != "GET"], [])
        self.assertTrue(any("include_ancestor_groups=true" in r["url"] for r in self.fake.requests))


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
                calls = []

                def run(cmd, **kw):  # nothing stored: git fails as it does without an entry
                    calls.append(cmd[0])
                    return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"")

                out, err = io.StringIO(), io.StringIO()
                code = fg.main(["gates", "--pr", self.PR[forge], *self.ARGS[forge]], env=dict(self.OVERRIDES),
                               stdout=out, stderr=err, opener=self.fake, run=run)
                self.assertEqual(code, 2)
                self.assertEqual(calls, ["git"], "Git's credential helper, never gh")

    def test_gitea_token_from_the_credential_helper(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="forge-gates-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stored = "stored-fixture-placeholder-token"
        env = tvd.git_env(tmp, FAKE_CREDENTIAL_HOST="git.example.org", FAKE_CREDENTIAL_TOKEN=stored)
        out, err = io.StringIO(), io.StringIO()
        fg.main(["gates", "--pr", self.PR["gitea"], *self.ARGS["gitea"]], env=env, stdout=out, stderr=err,
                opener=self.fake, run=subprocess.run)
        self.assertEqual(tvd.git_queries(tmp), [{"protocol": "https", "host": "git.example.org"}])
        self.assertNotIn(stored, out.getvalue() + err.getvalue())


class VerdictWord(unittest.TestCase):
    """gates reports the word under VERDICT the way verify-delivery reads it: blank lines may come between."""

    def test_blank_lines_before_the_word(self):
        for body in ("VERDICT\nRETURN_TO_IMPLEMENTATION\n", "VERDICT\n\nRETURN_TO_IMPLEMENTATION\n",
                     "VERDICT\r\n\r\n  \r\nRETURN_TO_IMPLEMENTATION\r\n"):
            with self.subTest(body=body):
                self.assertEqual(fg.VERDICT_WORD.search(body).group(1), "RETURN_TO_IMPLEMENTATION")

    def test_prose_is_not_the_word(self):
        self.assertIsNone(fg.VERDICT_WORD.search("VERDICT\nnot decided yet\n"))


class MergeCheck(unittest.TestCase):
    """merge-check is verify-delivery's document and exit code."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="forge-gates-"))
        (self.tmp / "token").write_text(TOKEN + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def both(self, forge, edit=None, extra=()):
        cfg = tvd.FORGES[forge]
        argv = ["--pr", cfg["pr"], *cfg["args"], *cfg.get("authors", []), *extra]
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


    def test_confirmed_after_repair_passes_through(self):
        class Repaired(tvd.Verify, tvd.RepairCases):
            pass

        def repaired(fake):
            case = Repaired()
            case.forge, case.fake = fake.forge, fake
            case.ci_fail()
            case.repair()

        for forge in ("github", "gitea"):
            with self.subTest(forge=forge):
                for extra, verdict, code in (((), "not_confirmed", 3),
                                             (("--ci-repaired-by", tvd.LATER), "confirmed_after_repair", 0)):
                    (vd_code, doc), (fg_code, fg_doc) = self.both(forge, repaired, extra)
                    self.assertEqual((fg_code, fg_doc), (vd_code, doc))
                    self.assertEqual((fg_code, fg_doc["verdict"]), (code, verdict))


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
