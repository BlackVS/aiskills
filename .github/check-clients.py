#!/usr/bin/env python3
"""Check that each agent client discovers what the installers write, without
calling a model.

    python3 .github/check-clients.py            # install from this checkout (git archive HEAD)
    python3 .github/check-clients.py --release  # install what the one-liner installs (latest release)
    python3 .github/check-clients.py --latest-clients  # each client's latest npm version instead of the pins

The clients are installed from npm at the versions pinned below into a work
directory. Three scenarios run in fresh home directories:

  user     the one-liner's defaults (--user -s all -p -a) in a home where Codex,
           Gemini CLI and OpenCode are already set up;
  claude   a Claude Code only user install (--user -t claude -s core), which
           OpenCode must find in ~/.claude/skills;
  project  a project install (<repo> --agents-md) seen from inside the repo.

Each client is asked what it discovered through its own model-free listing:
Claude Code's stream-json init event (the request after it goes to an
unreachable address with a dummy key), `codex debug prompt-input`, OpenCode
1.x `debug skill` / `debug config`, OpenCode v2's server API (`/api/skill`,
`/api/command`), `gemini skills list` and Gemini CLI's memory discovery log.
It exits non-zero when any check fails.
"""
import argparse, json, os, pathlib, re, shutil, signal, subprocess, sys, tempfile, threading, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENTS = {  # key: (npm package, executable inside the prefix)
    "claude": ("@anthropic-ai/claude-code@2.1.283", "bin/claude"),
    "codex": ("@openai/codex@0.157.1", "bin/codex"),
    "opencode1": ("opencode-ai@1.18.32", "bin/opencode"),
    "opencode2": ("@opencode/cli@2.0.18", "bin/opencode"),
    "gemini": ("@google/gemini-cli@0.61.0", "bin/gemini"),
}
CORE = {"architecture-review", "oh-code-review", "oh-technical-writing"}
MARKER = "ai-skills:review-gates start"
NOWHERE = "http://127.0.0.1:9"  # nothing listens there: a model request fails instead of being sent
results = []


def text(path):
    """A file's content, or "" when the installer did not write it (a failed check, not a crash)."""
    path = pathlib.Path(path)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def check(client, scenario, what, ok, detail=""):
    results.append((client, scenario, what, bool(ok), detail))


def run(cmd, home, cwd, timeout=180, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"):
        if k in os.environ:
            e[k] = os.environ[k]
    e.update(env or {})
    # stdout through a file: OpenCode 1.x exits before a pipe drains and cuts its output at 64 KiB
    with tempfile.TemporaryFile("w+") as out:
        r = subprocess.run(cmd, cwd=cwd, env=e, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout)
        out.seek(0)
        r.stdout = out.read()
    return r


def stream_until(cmd, home, cwd, env, done, timeout=90):
    """Run cmd, feed each output line to done(line) until it returns True or time runs out, then stop it."""
    e = {"PATH": os.environ["PATH"], "HOME": str(home), "TERM": "dumb", "LANG": "C.UTF-8", **env}
    p = subprocess.Popen(cmd, cwd=cwd, env=e, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    # a timer, not a check per line: a client that hangs silently must not outlive the deadline
    timer = threading.Timer(timeout, lambda: os.killpg(p.pid, signal.SIGTERM))
    timer.start()
    try:
        for line in p.stdout:
            if done(line):
                break
    finally:
        timer.cancel()
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        p.wait(timeout=30)


def install_clients(work, latest=False):
    bins = {}
    for key, (pkg, exe) in CLIENTS.items():
        if latest:
            pkg = pkg.rsplit("@", 1)[0] + "@latest"
        prefix = work / "clients" / (key + ("-latest" if latest else ""))
        if not (prefix / exe).exists():
            subprocess.run(["npm", "install", "-g", "--prefix", str(prefix), pkg], check=True,
                           stdout=subprocess.DEVNULL, timeout=900)
        bins[key] = str(prefix / exe)
    return bins


def boot(home, archive, *args, cwd=None):
    env = {"AI_SKILLS_ARCHIVE": str(archive)} if archive else {}
    r = run(["bash", str(ROOT / "boot.sh"), *args], home, cwd or home, env=env, timeout=300)
    if r.returncode:
        sys.exit(f"boot.sh {' '.join(args)} failed:\n{r.stdout}{r.stderr}")


def fresh_home(work, name, clients=()):
    home = work / name
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    for d in clients:
        (home / d).mkdir(parents=True)
    return home


def gemini_setup(home, *trusted):
    """Gemini CLI needs an auth method chosen before it starts, and reads a project only when trusted."""
    (home / ".gemini").mkdir(exist_ok=True)
    (home / ".gemini/settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}))
    (home / ".gemini/trustedFolders.json").write_text(json.dumps({str(t): "TRUST_FOLDER" for t in trusted}))


def git_repo(path):
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True, env={**os.environ, **env})
    return path


# ---- one lister per client: each returns what the client itself reports

def claude_skills(bins, home, cwd):
    found = {}
    def done(line):
        if line.startswith("{") and '"subtype":"init"' in line:
            found.update(json.loads(line))
            return True
        return False
    stream_until([bins["claude"], "-p", "hi", "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
                 home, cwd, {"ANTHROPIC_BASE_URL": NOWHERE, "ANTHROPIC_API_KEY": "dummy-not-a-key"}, done)
    return set(found.get("skills", []))


def codex_prompt(bins, home, cwd):
    r = run([bins["codex"], "debug", "prompt-input"], home, cwd)
    text = r.stdout
    block = text[text.find("<skills_instructions>"):text.find("</skills_instructions>")]
    return set(re.findall(r"\\n- ([a-z0-9-]+): ", block)), MARKER in text


def json_after(text, opener):
    return json.loads(text[text.index(opener):])


def opencode1(bins, home, cwd):
    skills = {s["name"] for s in json_after(run([bins["opencode1"], "debug", "skill"], home, cwd).stdout, "[")}
    config = json_after(run([bins["opencode1"], "debug", "config"], home, cwd).stdout, "{")
    return skills, set((config.get("command") or {}).keys())


def opencode2(bins, home, cwd, port, skills, commands=()):
    log = home / f"opencode-serve-{port}.log"
    with open(log, "w") as out:
        p = subprocess.Popen([bins["opencode2"], "serve", "--port", str(port)], cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, "HOME": str(home)})
    try:
        password = None
        for _ in range(60):
            m = re.search(r"^server password (\S+)", log.read_text(), re.M)
            if m:
                password = m.group(1)
                break
            time.sleep(1)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        auth = "Basic " + __import__("base64").b64encode(f"opencode:{password}".encode()).decode()
        def get(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}?location%5Bdirectory%5D={cwd}", headers={"Authorization": auth})
            return json.load(opener.open(req, timeout=30))["data"]
        def catalog(path, key, wanted):
            # each catalog loads in the background, built-ins first: poll until what we
            # expect is there or a minute has passed (then the check reports what is missing)
            found = set()
            for _ in range(30):
                found = {item[key] for item in get(path)}
                if set(wanted) <= found:
                    break
                time.sleep(2)
            return found
        return catalog("/api/skill", "id", skills), catalog("/api/command", "name", commands)
    finally:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=30)


def gemini_skills(bins, home, cwd):
    out = run([bins["gemini"], "skills", "list"], home, cwd).stdout
    return {m for m in re.findall(r"^(\S+) \[Enabled\](?! \[Built-in\])", out, re.M)}


def gemini_memory(bins, home, cwd, wanted):
    """The memory files Gemini CLI loads, with their length after imports are resolved,
    read from its debug log until `wanted` shows up (or the time runs out)."""
    files = {}
    def done(line):
        m = re.search(r"processed imports: (.+) \(Length: (\d+)\)", line)
        if m:
            files[m.group(1)] = int(m.group(2))
        return str(wanted) in files
    stream_until([bins["gemini"], "--debug", "-p", "hi"], home, cwd,
                 {"GEMINI_API_KEY": "dummy-not-a-key", "GOOGLE_GEMINI_BASE_URL": NOWHERE}, done, timeout=60)
    return files


def expect(client, scenario, what, found, wanted):
    missing = sorted(set(wanted) - set(found))
    check(client, scenario, what, not missing, f"missing {missing}" if missing else f"{len(wanted)} found")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--release", action="store_true", help="install the latest release with the one-liner instead of this checkout")
    ap.add_argument("--latest-clients", action="store_true", help="install each client's latest version instead of the pinned one")
    ap.add_argument("--workdir", type=pathlib.Path, help="where clients and scratch homes go (kept afterwards)")
    a = ap.parse_args()
    work = a.workdir or pathlib.Path(tempfile.mkdtemp(prefix="ai-skills-clients-"))
    work.mkdir(parents=True, exist_ok=True)
    archive = None
    if not a.release:
        archive = work / "ai-skills.tar.gz"
        # the working tree's tracked files, uncommitted edits included (stash create leaves the tree alone)
        tree = subprocess.run(["git", "stash", "create"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip() or "HEAD"
        subprocess.run(["git", "archive", "--format=tar.gz", "--prefix=ai-skills/", "-o", str(archive), tree], cwd=ROOT, check=True)
    print(f"work directory: {work}\ninstalling clients ...", flush=True)
    bins = install_clients(work, a.latest_clients)
    for key, exe in bins.items():
        v = run([exe, "--version"], work, work)
        print(f"  {key}: {' '.join((v.stdout or v.stderr).split()[-3:])}", flush=True)

    # user: the one-liner's defaults where Codex, Gemini CLI and OpenCode are set up
    home = fresh_home(work, "home-user", [".codex", ".config/opencode"])
    empty = work / "empty"; empty.mkdir(exist_ok=True)
    gemini_setup(home, empty)
    boot(home, archive)
    every = {p.name for p in (home / ".agents/skills").iterdir()}
    prompts = {p.stem for p in (home / ".claude/prompts").glob("*.md")}
    if archive:
        check("installer", "user", "all shipped skills installed", every == {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()})
    expect("claude", "user", "skills", claude_skills(bins, home, empty), every)
    check("claude", "user", "gates block in ~/.claude/CLAUDE.md", MARKER in text(home / ".claude/CLAUDE.md"))
    skills, gates = codex_prompt(bins, home, empty)
    expect("codex", "user", "skills", skills, every)
    check("codex", "user", "gates block in the prompt", gates)
    oc1_home, oc2_home = work / "home-user-oc1", work / "home-user-oc2"  # the two OpenCode versions cannot share a home
    for h in (oc1_home, oc2_home):
        shutil.rmtree(h, ignore_errors=True); shutil.copytree(home, h, symlinks=True)
    skills, commands = opencode1(bins, oc1_home, empty)
    expect("opencode 1.x", "user", "skills", skills, every)
    expect("opencode 1.x", "user", "prompts as commands", commands, prompts)
    skills, commands = opencode2(bins, oc2_home, empty, 47401, every, prompts)
    expect("opencode v2", "user", "skills", skills, every)
    expect("opencode v2", "user", "prompts as commands", commands, prompts)
    check("opencode", "user", "gates block in ~/.config/opencode/AGENTS.md", MARKER in text(home / ".config/opencode/AGENTS.md"))
    expect("gemini", "user", "skills", gemini_skills(bins, home, empty), every)
    gemini_md = str(home / ".gemini/GEMINI.md")
    memory = gemini_memory(bins, home, empty, gemini_md)
    check("gemini", "user", "loads ~/.gemini/GEMINI.md with the gates block",
          gemini_md in memory and MARKER in text(gemini_md), f"loaded: {sorted(memory)}")

    # claude: a Claude Code only install is enough for OpenCode
    for key, port in (("opencode1", None), ("opencode2", 47402)):
        home = fresh_home(work, f"home-claude-{key}")
        boot(home, archive, "--user", "-t", "claude", "-s", "core")
        check("installer", "claude", "no ~/.agents for a Claude-only install", not (home / ".agents").exists())
        skills = opencode1(bins, home, empty)[0] if port is None else opencode2(bins, home, empty, port, CORE)[0]
        expect("opencode 1.x" if port is None else "opencode v2", "claude", "skills from ~/.claude/skills", skills, CORE)

    # project: seen from inside the repository
    repo = git_repo(work / "repo")
    home = fresh_home(work, "home-project")
    gemini_setup(home, repo)
    boot(home, archive, str(repo), "--agents-md")
    expect("claude", "project", "skills", claude_skills(bins, home, repo), CORE)
    check("claude", "project", "CLAUDE.md imports AGENTS.md", "@AGENTS.md" in text(repo / "CLAUDE.md"))
    skills, gates = codex_prompt(bins, home, repo)
    expect("codex", "project", "skills", skills, CORE)
    check("codex", "project", "gates block in the prompt", gates)
    oc1_home, oc2_home = work / "home-project-oc1", work / "home-project-oc2"
    for h in (oc1_home, oc2_home):
        shutil.rmtree(h, ignore_errors=True); shutil.copytree(home, h, symlinks=True)
    expect("opencode 1.x", "project", "skills", opencode1(bins, oc1_home, repo)[0], CORE)
    expect("opencode v2", "project", "skills", opencode2(bins, oc2_home, repo, 47403, CORE)[0], CORE)
    expect("gemini", "project", "skills", gemini_skills(bins, home, repo), CORE)
    memory = gemini_memory(bins, home, repo, repo / "GEMINI.md")
    size = len(text(repo / "GEMINI.md"))
    loaded = memory.get(str(repo / "GEMINI.md"), 0)
    check("gemini", "project", "GEMINI.md resolves the @AGENTS.md import", loaded > size + len(MARKER),
          f"GEMINI.md is {size} bytes, loaded as {loaded}")

    width = max(len(f"{c} / {s} / {w}") for c, s, w, _, _ in results)
    for c, s, w, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {f'{c} / {s} / {w}':<{width}}  {detail}")
    failed = sum(not ok for *_, ok, _ in results)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
