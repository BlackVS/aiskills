---
name: aiforge-export
description: Write an aiforge export on a connected machine, the one directory a closed network installs aimem, aicrew and aiskills from. Downloads each release's SHA256SUMS and files (platform binaries only for the platforms chosen), verifies every file as it arrives, and writes MANIFEST, README.txt with the install commands and <product>/<tag>/ in format 1. Use when a network without access to GitHub needs these tools, or to refresh such an export.
---

# aiforge export

Use this skill to prepare the files a closed network installs from. Run it
on a machine that can reach GitHub; carry the directory it writes into the
closed network (removable media, a share, or an internal https server); the
installers there read it through their base variable (`AIMEM_BASE`,
`AICREW_BASE`, `AI_SKILLS_EXPORT`).

The helper is `aiforge-export` next to this file (Python 3.8 or newer,
standard library only):

```sh
python3 aiforge-export --out /media/aiforge-export --platforms linux-amd64,windows-amd64
python3 aiforge-export --out ./export --products aimem,aiskills --aimem v0.10.23
```

- `--out DIR` is the export to write. It must not exist yet. The export is
  built in a directory beside it and renamed into place only when every
  file has been verified, so a failed run leaves nothing at `DIR`.
- `--platforms` takes `os-arch` pairs (`linux-amd64`, `linux-arm64`,
  `darwin-arm64`, `windows-amd64`, ...). Without it every platform's binaries
  are taken. A platform a product publishes no binary for fails the run.
- `--products` chooses among `aimem`, `aicrew` and `aiskills` (default: all
  three). `--aimem`, `--aicrew` and `--aiskills` name a release by its tag;
  without one the latest release is taken.

What it refuses, each with a message naming the URL or file:

- a download that fails, or a file that does not match its `SHA256SUMS`
  entry;
- a `SHA256SUMS` line that is not `<sha256>  <file name>`, or a name that is
  not a plain file name;
- an aimem release whose sums list no `install.sh`, `install.ps1` or
  `aimem-opencode.ts` (releases before v0.10.23): its installers cannot
  install it from an export;
- any URL, redirects included, that is not https;
- an `--out` that already exists.

A release that publishes its files but not yet its installer scripts as
assets (aicrew's releases so far, aiskills' releases before the boot
scripts became assets) is still exported; `README.txt` says what is missing
and how to install from the verified files by hand.

The format, its six rules and what each installer does with it are in
`docs/aiforge-export.md` in the aiskills repository. Read it before
changing what the helper writes: three products' installers read this
layout.
