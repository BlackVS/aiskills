"""No shipped text names a host outside an allow-list.

The release archive is `git archive` of the tagged tree, so every tracked file ships
(less any marked export-ignore). A skill, prompt, script or test that names a private
host would carry it to every user, so each URL and each dotted host name in those files
must be a reserved example name (RFC 2606, RFC 6761), the loopback interface, a
documentation address (RFC 5737, RFC 3849), or a host in PUBLIC below, which says why
it may appear. A new public reference is added to that list, with its reason, in the
pull request that introduces it.

URLs are checked whatever their host looks like, an IPv6 literal included, and so is
every IPv4 address outside a URL. A host name outside a URL is recognised by its
top-level label (TLDS), in either case, since a dotted word in code is usually not a
host; the list holds the common public suffixes and the ones private networks use.
Case never exempts a name: `System.IO.Path` passes only because it is longer than a
host name ending in a listed label.

Run: python3 -m unittest tests/test_shipped_hosts.py
"""
import ipaddress, pathlib, re, subprocess, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

PUBLIC = {
    "github.com": "the forge this set is published on",
    "api.github.com": "its API: the boot scripts' release lookup, verify-delivery, forge-gates",
    "users.noreply.github.com": "the commit e-mail domain of the release workflow's bot",
    "raw.githubusercontent.com": "where the connected one-liners fetch the boot scripts",
    "gitea.com": "the public Gitea where the helpers' Gitea behaviour was observed",
    "gitlab.com": "the public GitLab where verify-delivery's GitLab fixtures were recorded",
    "api.openai.com": "the provider endpoint the review hook's example sites use",
    "developers.openai.com": "Codex documentation cited in README",
    "opencode.ai": "OpenCode documentation cited in README",
    "claude.ai": "a hosted agent service named in forge-gates and the changelog",
    "chatgpt.com": "a hosted agent service named in forge-gates",
    "chat.openai.com": "a hosted agent service named in forge-gates' session-link pattern and its tests",
    "claude.com": "the client's footer link, which check-links' tests show passing",
    "keepachangelog.com": "the changelog's format",
    "semver.org": "the versioning scheme",
    "polyformproject.org": "the license",
    "osv.dev": "an advisory source of the supply-chain checklist",
    "pypi.org": "a package registry the supply-chain checklist reads",
    "pypistats.org": "download statistics the supply-chain checklist reads",
    "www.npmjs.com": "a package registry the supply-chain checklist reads",
    "security.snyk.io": "an advisory source of the supply-chain checklist",
    "ghcr.io": "the container registry the readiness scanner looks for",
    "prose.ami.rip": "the source of the technical-writing skill",
    "criterion.rs": "the Rust benchmarking crate the readiness scanner looks for",
    "slsa.dev": "the build-provenance predicate type aiforge-export picks attestation bundles by",
    "in-toto.io": "the statement and release-attestation types in aiforge-export's attestation tests",
    # Names that only appear in tests of host checks: deliberately spoofed hosts under
    # an undelegated top-level label, which no one can register.
    "api.openai.com.evil": "a spoofed host in the review hook's host-check tests",
    "provider.example.evil": "a spoofed host in the review hook's host-check tests",
}

RESERVED = ("example", "test", "invalid", "localhost")  # RFC 2606 and RFC 6761 top-level names
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
# The address blocks reserved for documentation (RFC 5737, RFC 3849).
DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in
                           ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))

TLDS = ("com", "org", "net", "io", "dev", "ai", "app", "co", "cloud", "info", "biz", "xyz", "me",
        "tech", "site", "online", "ru", "cn", "de", "uk", "eu", "fr", "jp", "ua", "us",
        "local", "lan", "internal", "home", "corp", "intranet", "arpa", "example", "test", "invalid")
# A URL's host: after any user information, a bracketed IPv6 literal or a name or
# IPv4 address up to the port, path, query or the text around the URL.
URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/\s\"'<>(){}\[\]`?#\\@]*@)?"
                 r"(\[[^\]\s/]*\]|[^/\s\"'<>(){}\[\]`?#\\:@]*)")
# A dotted name ending in one of TLDS, in either case, and not the start of a longer
# dotted word (`settings.local.json` is a file name).
HOST = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + "|".join(TLDS) + r"))(?![\w-]|\.\w)",
                  re.I)
# A dotted-quad IPv4 address outside a URL (`user@<address>`, `HUB=<address>:8080`).
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\w|\.\d)")
# Attribute access in code (`self.dev`, `threading.local`) reads like a host name; it is not one.
NOT_HOSTS = re.compile(r"^(self|cls|threading)\.")



def allowed(host):
    host = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
        return ip.is_loopback or any(ip in n for n in DOCUMENTATION_NETS)
    except ValueError:
        pass
    if host.startswith("["):
        return False  # a bracketed host that is not an address
    if "." not in host:
        return True  # a placeholder (HOST, {host}, ...) or localhost, never a real name
    if host in PUBLIC:
        return True
    if any(host == d or host.endswith("." + d) for d in RESERVED_DOMAINS):
        return True
    return host.rsplit(".", 1)[1] in RESERVED


def unlisted(text):
    """Every URL host and dotted host name in text that the allow-list does not cover."""
    found = []
    for m in URL.finditer(text):
        host = m.group(1)
        if not allowed(host):
            found.append(host)
    text = URL.sub(lambda m: " " * len(m.group(0)), text)  # each URL's host is counted once
    for m in HOST.finditer(text):
        name = m.group(1)
        if not NOT_HOSTS.match(name) and not allowed(name):
            found.append(m.group(1))
    for m in IPV4.finditer(text):
        try:
            ipaddress.ip_address(m.group(1))
        except ValueError:
            continue  # four dotted numbers that are no address, such as 1.2.3.400
        if not allowed(m.group(1)):
            found.append(m.group(1))
    return found


def shipped_files():
    """The files `git archive` puts in a release, read from the working tree."""
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    names = [n for n in files.decode("utf-8").split("\0") if n]
    attrs = subprocess.run(["git", "check-attr", "-z", "export-ignore", "--stdin"], cwd=ROOT, check=True,
                           input="\0".join(names).encode("utf-8") + b"\0", capture_output=True).stdout
    fields = attrs.decode("utf-8").split("\0")
    ignored = {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "set"}
    return [n for n in names if n not in ignored]


class ShippedHosts(unittest.TestCase):
    def test_no_shipped_text_names_an_unlisted_host(self):
        files = shipped_files()
        self.assertIn("skills/oh-code-review/SKILL.md", files)
        problems, read = [], 0
        for name in files:
            try:
                text = (ROOT / name).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue  # not text, or deleted in the working tree
            read += 1
            for n, line in enumerate(text.splitlines(), 1):
                for host in unlisted(line):
                    problems.append(f"{name}:{n}: {host}")
        self.assertGreater(read, 100)
        self.assertEqual(problems, [], "add a public reference to PUBLIC with its reason, or use an example name")

    def test_a_planted_host_fails(self):
        # Built at run time: this file ships too, and must not name them itself.
        def h(*labels):
            return ".".join(labels)
        for text, host in ((f"see https://{h('gitea', 'corp-internal', 'dev')}/x/y", h("gitea", "corp-internal", "dev")),
                           (f"curl -fsSL http://{h('10', '1', '2', '3')}:8080/boot.sh", h("10", "1", "2", "3")),
                           (f"AIMEM_HUB={h('hub', 'lan')}", h("hub", "lan")),
                           (f"push to git@{h('forge', 'company', 'com')}:team/repo.git", h("forge", "company", "com")),
                           (f"https://user:secret@{h('build', 'internal')}:443/", h("build", "internal")),
                           (f"the mirror at {h('files', 'home')}", h("files", "home")),
                           (f"wss://{h('relay', 'example', 'co')}/", h("relay", "example", "co")),
                           (f"see {h('GITEA', 'CORP', 'LAN')} for the mirror", h("GITEA", "CORP", "LAN")),
                           (f"mirror at {h('Build', 'company', 'com')}", h("Build", "company", "com")),
                           (f"see {h('System', 'Net')}", h("System", "Net")),
                           (f"https://[{':'.join(('2001', '4860', '', '8888'))}]:443/x", f"[{':'.join(('2001', '4860', '', '8888'))}]"),
                           (f"http://[{':'.join(('fe80', '', '1'))}]/", f"[{':'.join(('fe80', '', '1'))}]"),
                           (f"https://[{h('not', 'an', 'address')}]/", f"[{h('not', 'an', 'address')}]"),
                           (f"ssh deploy@{h('10', '20', '30', '40')}", h("10", "20", "30", "40")),
                           (f"HUB={h('172', '16', '0', '9')}:8080", h("172", "16", "0", "9"))):
            with self.subTest(text=text):
                self.assertEqual(unlisted(text), [host])

    def test_allowed_names_pass(self):
        for text in ("https://github.com/BlackVS/aiskills", "https://HOST/api/v1", "https://{host}/x",
                     "http://127.0.0.1:8000/", "http://[::1]:9/", "http://192.0.2.1/x", "https://[2001:db8::1]:8443/x", "System.Net.Http",
                     "https://user:pass@example.com/x", "bind 127.0.0.1:8080", "HUB=192.0.2.10", "v1.2.3.400",
                     "version 1.38.0", "https://git.example.org/acme", "forge.example",
                     "files.example.invalid", "example.test", "localhost:3000", "see pypi.org", "self.dev / 'x'",
                     "System.IO.Path", "install.sh and boot.ps1", "subprocess.run(x)", "e.g. one.two",
                     "settings.local.json", "threading.local()", "${BASE:-https://github.com}"):
            with self.subTest(text=text):
                self.assertEqual(unlisted(text), [])

    def test_every_public_host_has_a_reason(self):
        for host, why in PUBLIC.items():
            self.assertTrue(why.strip(), host)


if __name__ == "__main__":
    unittest.main()
