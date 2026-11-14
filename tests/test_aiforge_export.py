"""skills/aiforge-export/aiforge-export against a fake release server on the loopback
interface, and once against the real aimem v0.10.23 release.

Run: python3 -m unittest tests/test_aiforge_export.py
"""
import hashlib, http.server, importlib.machinery, importlib.util, json, os, pathlib, subprocess, sys
import contextlib, io, stat, tempfile, threading, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "aiforge-export" / "aiforge-export"

sys.dont_write_bytecode = True  # no __pycache__ inside skills/: the installers copy the folder as it is
_loader = importlib.machinery.SourceFileLoader("aiforge_export", str(SCRIPT))
_spec = importlib.util.spec_from_loader("aiforge_export", _loader)
ax = importlib.util.module_from_spec(_spec)
_loader.exec_module(ax)


def sums_of(files):
    return "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in sorted(files.items())).encode()


def acl(path):
    """A Windows access list as icacls prints it, without the path and the summary line."""
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    lines = [line.strip() for line in out.splitlines() if line.strip()][:-1]
    lines[0] = lines[0][len(str(path)):].strip()
    return lines


def release(product, tag, names):
    return {n: f"{product} {tag} {n}\n".encode() for n in names}


AIMEM = ["LICENSE", "aimem-darwin-arm64", "aimem-linux-amd64", "aimem-linux-arm64", "aimem-opencode.ts",
         "aimem-windows-amd64.exe", "boot.ps1", "boot.sh", "install-hub.sh", "install.ps1", "install.sh"]
AICREW = ["LICENSE", "aicrew-agent-linux-amd64", "aicrew-agent-windows-amd64.exe", "aicrew-linux-amd64",
          "aicrew-windows-amd64.exe", "aicrewd-darwin-arm64", "aicrewd-linux-amd64"]
AISKILLS = ["aiskills-1.39.0.tar.gz", "aiskills-1.39.0.zip", "boot.ps1", "boot.sh"]


class FakeForge:
    """Serves releases as GitHub lays them out, under /dl (downloads) and /api (the
    latest-release lookup), and records every path asked for."""

    def __init__(self):
        self.releases, self.latest, self.sums, self.paths = {}, {}, {}, []
        self.redirect_to = None
        self.cut_short = None  # a file name whose body is sent short of its Content-Length
        forge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                forge.paths.append(self.path)
                status, headers, body = forge.answer(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                short = forge.cut_short and self.path.endswith("/" + forge.cut_short)
                self.send_header("Content-Length", str(len(body) + (100 if short else 0)))
                if short:
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add(self, product, tag, files, latest=True, sums=None):
        self.releases[(product, tag)] = files
        self.sums[(product, tag)] = sums_of(files) if sums is None else sums
        if latest:
            self.latest[product] = tag

    def answer(self, path):
        parts = path.split("/")
        if parts[1:3] == ["api", "repos"] and parts[3] == "BlackVS" and parts[5:] == ["releases", "latest"]:
            tag = self.latest.get(parts[4])
            return (200, {}, json.dumps({"tag_name": tag}).encode()) if tag else (404, {}, b"")
        if parts[1] == "dl" and parts[2] == "BlackVS" and parts[4:6] == ["releases", "download"] and len(parts) == 8:
            key, name = (parts[3], parts[6]), parts[7]
            if self.redirect_to and name == self.redirect_to[0]:
                return 302, {"Location": self.redirect_to[1]}, b""
            if key in self.releases and name == "SHA256SUMS":
                return 200, {}, self.sums[key]
            if name in self.releases.get(key, {}):
                return 200, {}, self.releases[key][name]
        if parts[1] == "moved":
            product, tag, name = parts[2:5]
            return 200, {}, self.releases[(product, tag)][name]
        return 404, {}, b""


class ExportCases(unittest.TestCase):
    def setUp(self):
        self.forge = FakeForge()
        self.addCleanup(self.forge.close)
        self.forge.add("aimem", "v0.10.23", release("aimem", "v0.10.23", AIMEM))
        self.forge.add("aicrew", "v0.9.0", release("aicrew", "v0.9.0", AICREW))
        self.forge.add("aiskills", "v1.39.0", release("aiskills", "v1.39.0", AISKILLS))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.out = self.tmp / "export"

    def run_export(self, *args):
        cmd = [sys.executable, str(SCRIPT), "--out", str(self.out), "--download-base", self.forge.base + "/dl",
               "--api-base", self.forge.base + "/api", *args]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)

    def assert_nothing_written(self, r, *needles):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        for n in needles:
            self.assertIn(n, r.stderr)
        self.assertIn("nothing was written", r.stderr)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [], "no export and no partial directory")

    def test_writes_the_layout_with_the_chosen_platforms(self):
        r = self.run_export("--platforms", "linux-amd64,windows-amd64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.out / "MANIFEST").read_bytes(),
                         b"aiforge-export 1\naimem v0.10.23\naicrew v0.9.0\naiskills v1.39.0\n")
        for (product, tag), files in self.forge.releases.items():
            d = self.out / product / tag
            self.assertEqual((d / "SHA256SUMS").read_bytes(), self.forge.sums[(product, tag)], "kept whole")
            kept = {n for n in files if not any(p in n for p in ("darwin", "linux-arm64"))}
            self.assertEqual({p.name for p in d.iterdir()}, kept | {"SHA256SUMS"})
            for n in kept:
                self.assertEqual((d / n).read_bytes(), files[n], "unchanged")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["MANIFEST", "README.txt", "aicrew", "aimem", "aiskills"])
        self.assertIn("checksum OK: aimem/v0.10.23/aimem-linux-amd64", r.stdout)

    def test_all_platforms_by_default(self):
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.out / "aimem" / "v0.10.23" / "aimem-darwin-arm64").is_file())
        self.assertTrue((self.out / "aicrew" / "v0.9.0" / "aicrewd-darwin-arm64").is_file())

    def assert_commands_run_where_they_are(self, text):
        """No command hands a URL to bash or to -File: from a directory bash runs the
        script in place, and from an https server the script is piped to bash (with its
        arguments after -s --), as the connected one-liners are."""
        seen = 0
        for line in text.splitlines():
            words = line.split()
            if "bash" in words:
                seen += 1
                rest = words[words.index("bash") + 1:]
                if "<url>" in line:
                    self.assertEqual(words[:2], ["curl", "-fsSL"], line)
                    self.assertTrue(words[2].startswith("<url>/") and words[3] == "|", line)
                    self.assertIn(rest[:2], ([], ["-s", "--"]), line)
                else:
                    self.assertTrue(rest[0].startswith("<dir>/"), line)
            if "-File" in words:
                self.assertTrue(words[words.index("-File") + 1].startswith("<dir>\\"), line)
            if "iex" in words:
                self.assertEqual(words[:2], ["irm", words[1]], line)
                self.assertTrue(words[1].startswith("<url>/"), line)
        self.assertGreater(seen, 0)

    def test_readme_names_the_tags_and_no_forge(self):
        r = self.run_export("--platforms", "linux-amd64")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.out / "README.txt").read_text(encoding="utf-8")
        self.assertNotIn("github", text.lower())
        self.assertNotIn("://", text)
        for line in ("AIMEM_BASE=<dir> bash <dir>/aimem/v0.10.23/boot.sh",
                     "AIMEM_BASE=<dir> bash <dir>/aimem/v0.10.23/install-hub.sh",
                     "-File <dir>\\aimem\\v0.10.23\\boot.ps1",
                     "curl -fsSL <url>/aimem/v0.10.23/boot.sh | AIMEM_BASE=<url> bash\n",
                     "curl -fsSL <url>/aimem/v0.10.23/install-hub.sh | AIMEM_BASE=<url> bash\n",
                     "$env:AIMEM_BASE = '<url>'",
                     "irm <url>/aimem/v0.10.23/boot.ps1 | iex",
                     "AI_SKILLS_EXPORT=<dir> bash <dir>/aiskills/v1.39.0/boot.sh --user -s core",
                     "-File <dir>\\aiskills\\v1.39.0\\boot.ps1",
                     "curl -fsSL <url>/aiskills/v1.39.0/boot.sh | AI_SKILLS_EXPORT=<url> bash\n",
                     "curl -fsSL <url>/aiskills/v1.39.0/boot.sh | AI_SKILLS_EXPORT=<url> bash -s -- --user -s core",
                     "irm <url>/aiskills/v1.39.0/boot.ps1 | iex",
                     "aimem process export <repository> <commit> <manifest>",
                     "--from-archive <archive> --sums SHA256SUMS",
                     "for linux-amd64 only"):
            self.assertIn(line, text)
        self.assert_commands_run_where_they_are(text)
        # aicrew v0.9.0 publishes no installer scripts as assets: README.txt says so
        self.assertIn("does not publish boot.sh, boot.ps1, install-aicrewd.sh as release assets", text)
        self.assertIn("cd <dir>/aicrew/v0.9.0 && sha256sum -c --ignore-missing SHA256SUMS", text)
        self.assertNotIn("AICREW_BASE", text)

    def test_readme_for_releases_that_publish_their_installers(self):
        self.forge.add("aicrew", "v0.10.0", release("aicrew", "v0.10.0", AICREW + ["boot.sh", "boot.ps1", "install-aicrewd.sh"]))
        self.forge.add("aiskills", "v1.38.0", release("aiskills", "v1.38.0", ["aiskills-1.38.0.tar.gz", "aiskills-1.38.0.zip"]))
        r = self.run_export("--aiskills", "v1.38.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.out / "README.txt").read_text(encoding="utf-8")
        self.assertIn("AICREW_BASE=<dir> bash <dir>/aicrew/v0.10.0/install-aicrewd.sh", text)
        # aicrew's installers do not document the https form yet: README.txt says so
        self.assertIn("From an https server: aicrew's installers do not document that form yet;", text)
        self.assertNotIn("<url>/aicrew", text)
        self.assertIn("does not publish boot.sh, boot.ps1 as release assets", text)
        self.assertIn("tar -xzf aiskills-1.38.0.tar.gz && ./aiskills-1.38.0/install.sh --user", text)
        self.assertNotIn("AI_SKILLS_EXPORT", text)
        self.assert_commands_run_where_they_are(text)

    def test_latest_is_looked_up_and_explicit_tags_win(self):
        self.forge.add("aimem", "v0.10.24", release("aimem", "v0.10.24", AIMEM))
        r = self.run_export("--products", "aimem,aiskills", "--aimem", "v0.10.23", "--platforms", "linux-amd64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.out / "MANIFEST").read_text(), "aiforge-export 1\naimem v0.10.23\naiskills v1.39.0\n")
        self.assertIn("/api/repos/BlackVS/aiskills/releases/latest", self.forge.paths)
        self.assertNotIn("/api/repos/BlackVS/aimem/releases/latest", self.forge.paths)
        self.assertFalse(any("aicrew" in p for p in self.forge.paths))

    def test_follows_a_redirect_like_the_forge_does(self):
        self.forge.redirect_to = ("aimem-linux-amd64", self.forge.base + "/moved/aimem/v0.10.23/aimem-linux-amd64")
        r = self.run_export("--products", "aimem", "--platforms", "linux-amd64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/moved/aimem/v0.10.23/aimem-linux-amd64", self.forge.paths)

    def test_a_redirect_off_https_is_refused(self):
        self.forge.redirect_to = ("aimem-linux-amd64", "http://files.example.invalid/aimem-linux-amd64")
        r = self.run_export("--products", "aimem", "--platforms", "linux-amd64")
        self.assert_nothing_written(r, "redirects to http://files.example.invalid/aimem-linux-amd64, which is not https")

    def test_a_sum_mismatch_writes_nothing(self):
        files = release("aiskills", "v1.39.0", AISKILLS)
        sums = sums_of(files)
        files["boot.sh"] = b"tampered\n"
        self.forge.add("aiskills", "v1.39.0", files, sums=sums)
        r = self.run_export("--platforms", "linux-amd64")
        self.assert_nothing_written(r, "/aiskills/releases/download/v1.39.0/boot.sh does not match its SHA256SUMS entry")

    def test_a_missing_asset_writes_nothing(self):
        files = release("aicrew", "v0.9.0", AICREW)
        sums = sums_of(files)
        del files["LICENSE"]
        self.forge.add("aicrew", "v0.9.0", files, sums=sums)
        r = self.run_export("--platforms", "linux-amd64")
        self.assert_nothing_written(r, "/aicrew/releases/download/v0.9.0/LICENSE: HTTP 404")

    def test_an_aimem_release_without_installer_assets_is_refused(self):
        self.forge.add("aimem", "v0.10.22", release("aimem", "v0.10.22", [n for n in AIMEM if not n.startswith("install")
                                                                       and n != "aimem-opencode.ts"]))
        r = self.run_export("--aimem", "v0.10.22")
        self.assert_nothing_written(r, "aimem v0.10.22 cannot be installed from an export",
                                    "lists no install.sh, install.ps1, aimem-opencode.ts")

    def test_a_platform_with_no_binary_is_refused(self):
        r = self.run_export("--platforms", "linux-amd64,darwin-amd64")
        self.assert_nothing_written(r, "aimem v0.10.23 publishes no binary for darwin-amd64")

    def test_a_sums_file_naming_a_path_is_refused(self):
        files = release("aiskills", "v1.39.0", AISKILLS)
        self.forge.add("aiskills", "v1.39.0", files, sums=sums_of(files) + b"0" * 64 + b"  ../escape\n")
        r = self.run_export("--products", "aiskills")
        self.assert_nothing_written(r, "is not '<sha256>  <file name>'")

    def test_a_sums_file_listing_itself_is_kept_as_fetched(self):
        files = release("aiskills", "v1.39.0", AISKILLS)
        sums = sums_of(files) + b"0" * 64 + b"  SHA256SUMS\n"
        self.forge.add("aiskills", "v1.39.0", files, sums=sums)
        r = self.run_export("--products", "aiskills")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.out / "aiskills" / "v1.39.0" / "SHA256SUMS").read_bytes(), sums)

    def test_a_body_cut_short_writes_nothing(self):
        # A whole-body read (SHA256SUMS) that ends early; a streamed file that ends early
        # fails its checksum instead.
        self.forge.cut_short = "SHA256SUMS"
        r = self.run_export("--products", "aiskills")
        self.assert_nothing_written(r, "/aiskills/releases/download/v1.39.0/SHA256SUMS:")
        self.assertNotIn("Traceback", r.stderr)

    def test_an_os_error_is_reported_not_raised(self):
        args = ["--out", str(self.out), "--products", "aiskills", "--aiskills", "v1.39.0",
                "--download-base", self.forge.base + "/dl", "--api-base", self.forge.base + "/api"]
        err = io.StringIO()
        with mock.patch.object(ax.os, "mkdir", side_effect=PermissionError(13, "Permission denied", str(self.tmp))), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ax.main(args), 1)
        self.assertIn("aiforge-export: [Errno 13] Permission denied", err.getvalue())
        self.assertIn("nothing was written", err.getvalue())

    def test_the_export_is_readable_as_any_new_directory_is(self):
        # The export root and its files get what an ordinary new directory and file get
        # here, not a directory private to the account that wrote it.
        r = self.run_export("--products", "aiskills")
        self.assertEqual(r.returncode, 0, r.stderr)
        plain = self.tmp / "plain"
        os.mkdir(plain)
        (plain / "f").write_bytes(b"x")
        exported = self.out / "aiskills" / "v1.39.0" / "boot.sh"
        if os.name == "nt":
            self.assertEqual(acl(self.out), acl(plain))
            self.assertEqual(acl(exported), acl(plain / "f"))
        else:
            self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), stat.S_IMODE(plain.stat().st_mode))
            self.assertEqual(stat.S_IMODE(exported.stat().st_mode), stat.S_IMODE((plain / "f").stat().st_mode))

    def test_an_existing_out_is_refused(self):
        self.out.mkdir()
        r = self.run_export()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(self.forge.paths, [])

    def test_arguments_are_checked(self):
        for args, msg in ((["--download-base", "http://releases.example.invalid"], "is not an https URL"),
                          (["--platforms", "linux"], "is not a platform"),
                          (["--products", "aimem,other"], "is not a product"),
                          (["--aimem", "latest"], "is not a release tag")):
            with self.subTest(args=args):
                r = self.run_export(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn(msg, r.stderr)
        self.assertEqual(self.forge.paths, [])


class Rules(unittest.TestCase):
    def test_allowed_urls(self):
        for url, ok in (("https://github.com/x", True), ("http://127.0.0.1:8/x", True), ("http://localhost/x", True),
                        ("http://[::1]/x", True), ("http://github.com/x", False), ("file:///etc/passwd", False),
                        ("ftp://example.org/x", False), ("http://192.0.2.1/x", False)):
            self.assertEqual(ax.allowed_url(url), ok, url)

    def test_binaries_are_told_from_other_files(self):
        for name, platform in (("aimem-linux-amd64", "linux-amd64"), ("aicrew-agent-windows-amd64.exe", "windows-amd64"),
                               ("aicrewd-darwin-arm64", "darwin-arm64"), ("boot.sh", None), ("aimem-opencode.ts", None),
                               ("aiskills-1.39.0.tar.gz", None)):
            m = ax.BINARY.match(name)
            self.assertEqual(m.group(1) if m else None, platform, name)


@unittest.skipIf(os.environ.get("AI_SKILLS_OFFLINE") == "1", "AI_SKILLS_OFFLINE=1")
class RealRelease(unittest.TestCase):
    """One fetch of the real aimem v0.10.23, checked against the shape aimem's own
    installer tests use for an export (internal/installer/closed_network_test.go)."""

    def test_aimem_v0_10_23(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "export"
            r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--products", "aimem",
                                "--aimem", "v0.10.23", "--platforms", "linux-amd64"],
                               capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((out / "MANIFEST").read_text(), "aiforge-export 1\naimem v0.10.23\n")
            d = out / "aimem" / "v0.10.23"
            names = {p.name for p in d.iterdir()}
            for n in ("SHA256SUMS", "install.sh", "install.ps1", "aimem-opencode.ts", "boot.sh", "boot.ps1",
                      "install-hub.sh", "aimem-linux-amd64"):
                self.assertIn(n, names)
            self.assertFalse(any(n.startswith("aimem-") and n.endswith((".exe", "-arm64")) for n in names), names)
            sums = ax.parse_sums((d / "SHA256SUMS").read_text(), "SHA256SUMS")
            self.assertIn("aimem-windows-amd64.exe", sums, "SHA256SUMS is kept whole")
            for n in names - {"SHA256SUMS"}:
                self.assertEqual(hashlib.sha256((d / n).read_bytes()).hexdigest(), sums[n], n)
            self.assertNotIn("github", (out / "README.txt").read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
