"""The release script (.github/release.sh) against scratch repositories: a bare
origin, a maintainer clone that pushes versions, and the runner's clone that
the script works in, with a fake gh that records what it would publish.

Run: python3 -m unittest tests/test_release.py
"""
import hashlib, os, pathlib, shutil, subprocess, tempfile, unittest

from tests.test_install import BASH, ROOT

SCRIPT = ROOT / ".github" / "release.sh"
FAKE_GH = r"""#!/bin/sh
# "release view TAG" succeeds for the tags in FAKE_GH_RELEASES; "release create" is recorded.
echo "$*" >> "$FAKE_GH_LOG"
if [ "$1 $2" = "release view" ]; then
  for t in $FAKE_GH_RELEASES; do [ "$t" = "$3" ] && exit 0; done
  exit 1
fi
exit 0
"""
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class ScriptModes(unittest.TestCase):
    """Every tracked file that starts with #! is stored executable, so the release archives (built
    by git archive, which keeps the stored mode) run the documented commands as written. The
    release refuses a commit that breaks this; this test catches it on the pull request."""

    def test_scripts_are_stored_executable(self):
        staged = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        missing = []
        for entry in filter(None, staged.split(b"\0")):
            meta, path = entry.split(b"\t", 1)
            if meta.startswith(b"100644") and (ROOT / path.decode()).read_bytes()[:2] == b"#!":
                missing.append(path.decode())
        self.assertEqual(missing, [], "store them with git update-index --chmod=+x")


@unittest.skipUnless(BASH and os.name != "nt", "needs a POSIX bash to put a fake gh on PATH")
class ReleaseScript(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="aiskills-release-"))
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH); (self.bin / "gh").chmod(0o755)
        self.gh_log = self.tmp / "gh.log"
        self.env = dict(os.environ, **GIT_ENV, PATH=f"{self.bin}:{os.environ['PATH']}",
                        FAKE_GH_LOG=str(self.gh_log), FAKE_GH_RELEASES="")
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.dev = self.tmp / "dev"
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.dev))
        self.versions = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def commit_version(self, ver, section=True, tag=False):
        """The maintainer merges a version bump to main (and optionally tags it)."""
        self.versions.insert(0, ver)
        (self.dev / "VERSION").write_text(ver + "\n")
        body = "".join(f"## [{v}] - 2026-01-01\n\n### Changed\n- change {v}\n\n" for v in self.versions if section or v != ver)
        (self.dev / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n" + body)
        self.git(self.dev, "add", "-A"); self.git(self.dev, "commit", "-q", "-m", ver)
        self.git(self.dev, "push", "-q", "origin", "main")
        if tag:
            self.git(self.dev, "tag", "-a", f"v{ver}", "-m", ver); self.git(self.dev, "push", "-q", "origin", f"v{ver}")
        return self.git(self.dev, "rev-parse", "HEAD")

    def runner(self):
        """The workflow's checkout: a fresh clone of origin at this moment."""
        path = self.tmp / f"runner{len(list(self.tmp.glob('runner*')))}"
        self.git(self.tmp, "clone", "-q", str(self.origin), str(path))
        return path

    def release(self, clone, cmd, tag, *args, ok=True):
        r = subprocess.run([BASH, str(SCRIPT), cmd, tag, *args], cwd=clone, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r

    def created(self):
        return [line.split() for line in self.gh_log.read_text().splitlines() if line.startswith("release create")]

    def test_manual_release_tags_main_and_publishes(self):
        self.commit_version("1.1.0", tag=True)
        head = self.commit_version("1.2.0")
        run = self.runner()
        self.release(run, "prepare", "v1.2.0", "--create")
        self.assertIn("- change 1.2.0", (run / "release_notes.md").read_text())
        self.assertNotIn("1.1.0", (run / "release_notes.md").read_text())
        self.release(run, "publish", "v1.2.0", "--create")
        self.assertEqual(self.git(self.origin, "rev-parse", "v1.2.0^{commit}"), head, "the tag is pushed at main's tip")
        self.assertEqual(self.git(self.origin, "cat-file", "-t", "v1.2.0"), "tag", "an annotated tag")
        [args] = self.created()
        self.assertIn("--latest=true", args)
        self.assertIn("--verify-tag", args)
        dist = run / "dist"
        self.assertEqual(sorted(p.name for p in dist.iterdir()), ["SHA256SUMS", "aiskills-1.2.0.tar.gz", "aiskills-1.2.0.zip"])
        for line in (dist / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(maxsplit=1)
            self.assertEqual(hashlib.sha256((dist / name.lstrip("*")).read_bytes()).hexdigest(), digest)

    def test_pushed_tag_is_published_even_after_main_moved_on(self):
        tagged = self.commit_version("1.2.0", tag=True)
        self.commit_version("1.3.0")
        run = self.runner(); self.git(run, "checkout", "-q", "v1.2.0")
        self.release(run, "prepare", "v1.2.0")
        self.assertEqual(self.git(run, "rev-parse", "HEAD"), tagged)
        self.release(run, "publish", "v1.2.0")
        [args] = self.created()
        self.assertIn("--latest=true", args, "no higher tag exists yet")

    def test_commit_names_what_prepare_checks_out(self):
        tagged = self.commit_version("1.2.0", tag=True)
        head = self.commit_version("1.3.0")
        run = self.runner()
        self.assertEqual(self.release(run, "commit", "v1.2.0").stdout.strip(), tagged)
        self.assertEqual(self.release(run, "commit", "v1.3.0", "--create").stdout.strip(), head)
        self.assertIn("tag v1.3.0 does not exist", self.release(run, "commit", "v1.3.0", ok=False).stderr)
        self.release(run, "prepare", "v1.3.0", "--create")
        self.assertEqual(self.git(run, "rev-parse", "HEAD"), head)

    def test_stale_snapshot_does_not_take_latest(self):
        # The v1.2.0 run checks out before v1.3.0 exists; v1.3.0 is tagged and
        # published while it waits. It must not mark v1.2.0 latest.
        self.commit_version("1.2.0", tag=True)
        run = self.runner()
        self.release(run, "prepare", "v1.2.0")
        self.commit_version("1.3.0", tag=True)
        self.release(run, "publish", "v1.2.0")
        [args] = self.created()
        self.assertIn("--latest=false", args)
        # and the newer release, published in either order, takes latest
        run2 = self.runner()
        self.release(run2, "prepare", "v1.3.0"); self.release(run2, "publish", "v1.3.0")
        self.assertIn("--latest=true", self.created()[1])

    def test_patch_for_an_older_line_does_not_take_latest(self):
        self.commit_version("1.3.0", tag=True)
        self.commit_version("1.2.2")  # a later release that carries a lower version
        run = self.runner()
        self.release(run, "prepare", "v1.2.2", "--create"); self.release(run, "publish", "v1.2.2", "--create")
        self.assertIn("--latest=false", self.created()[0], "v1.3.0 stays latest")

    def test_script_without_execute_bit_is_refused(self):
        (self.dev / "tool.sh").write_text("#!/bin/sh\necho ok\n")
        (self.dev / "tool.sh").chmod(0o644)
        (self.dev / "notes.txt").write_text("plain text, no #! line\n")
        self.commit_version("1.2.0")
        r = self.release(self.runner(), "prepare", "v1.2.0", "--create", ok=False)
        self.assertIn("scripts stored without the execute bit: tool.sh", r.stderr)
        (self.dev / "tool.sh").chmod(0o755)  # what update-index --chmod=+x stores where the file system has no modes
        self.commit_version("1.2.1")
        run = self.runner()
        self.release(run, "prepare", "v1.2.1", "--create"); self.release(run, "publish", "v1.2.1", "--create")
        listing = subprocess.run(["tar", "-tvzf", str(run / "dist" / "aiskills-1.2.1.tar.gz")], capture_output=True,
                                 text=True, check=True).stdout
        [line] = [line for line in listing.splitlines() if line.endswith("aiskills-1.2.1/tool.sh")]
        self.assertTrue(line.startswith("-rwxr-xr-x"), line)

    def test_refusals(self):
        self.commit_version("1.2.0")
        run = self.runner()
        cases = {
            "not vMAJOR.MINOR.PATCH": ("prepare", "v1.2.0-rc1", "--create"),
            "tag v1.2.0 does not exist": ("prepare", "v1.2.0"),
            "VERSION at": ("prepare", "v1.3.0", "--create"),
        }
        for message, args in cases.items():
            with self.subTest(message):
                r = self.release(run, *args, ok=False)
                self.assertIn(message, r.stderr)
        self.env["FAKE_GH_RELEASES"] = "v1.2.0"
        self.assertIn("release v1.2.0 already exists", self.release(run, "prepare", "v1.2.0", "--create", ok=False).stderr)
        self.env["FAKE_GH_RELEASES"] = ""
        self.assertFalse(self.gh_log.exists() and self.created(), "nothing was published")
        self.assertEqual(self.git(self.origin, "tag", "-l"), "", "no tag was pushed")

    def resolve(self, ok=True, **event):
        env_file = self.tmp / "github_env"; env_file.write_text("")
        env = {k: v for k, v in self.env.items() if not k.startswith(("GITHUB_", "INPUT_"))}
        env.update(event, GITHUB_ENV=str(env_file))
        r = subprocess.run([BASH, str(SCRIPT), "resolve"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r, env_file.read_text()

    def test_resolve_manual_run(self):
        for version in ("1.22.1", "v1.22.1"):
            with self.subTest(version=version):
                r, written = self.resolve(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main", INPUT_VERSION=version)
                self.assertEqual(written, "TAG=v1.22.1\nCREATE=--create\n")

    def test_resolve_tag_push(self):
        r, written = self.resolve(GITHUB_EVENT_NAME="push", GITHUB_REF="refs/tags/v1.22.1", GITHUB_REF_NAME="v1.22.1")
        self.assertEqual(written, "TAG=v1.22.1\nCREATE=\n")

    def test_resolve_refuses_bad_input_and_writes_nothing(self):
        bad = {
            "a second line": "1.22.1\nTAG=v1.22.0",   # one valid line must not carry another into $GITHUB_ENV
            "a trailing newline": "1.22.1\n",
            "a partial version": "1.22",
            "a suffix": "1.22.1-rc1",
            "empty": "",
        }
        for name, version in bad.items():
            with self.subTest(name):
                r, written = self.resolve(ok=False, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main", INPUT_VERSION=version)
                self.assertIn("is not MAJOR.MINOR.PATCH", r.stderr)
                self.assertEqual(written, "")
        r, written = self.resolve(ok=False, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/side", INPUT_VERSION="1.22.1")
        self.assertIn("run the release from main", r.stderr); self.assertEqual(written, "")
        r, written = self.resolve(ok=False, GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="v1.22")
        self.assertEqual(written, "")

    def test_multiline_tag_argument_is_refused(self):
        self.commit_version("1.2.0")
        r = self.release(self.runner(), "prepare", "v1.2.0\nv9.9.9", "--create", ok=False)
        self.assertIn("is not vMAJOR.MINOR.PATCH", r.stderr)

    def test_missing_changelog_section_is_refused(self):
        self.commit_version("1.2.0", section=False)
        r = self.release(self.runner(), "prepare", "v1.2.0", "--create", ok=False)
        self.assertIn("CHANGELOG.md has no section for 1.2.0", r.stderr)

    def test_tag_off_main_is_refused(self):
        self.commit_version("1.2.0")
        self.git(self.dev, "checkout", "-q", "-b", "side")
        (self.dev / "x").write_text("x"); self.git(self.dev, "add", "x"); self.git(self.dev, "commit", "-q", "-m", "side")
        self.git(self.dev, "tag", "-a", "v1.2.0", "-m", "side"); self.git(self.dev, "push", "-q", "origin", "v1.2.0")
        r = self.release(self.runner(), "prepare", "v1.2.0", ok=False)
        self.assertIn("which is not on main", r.stderr)


if __name__ == "__main__":
    unittest.main()
