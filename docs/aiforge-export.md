# aiforge export, format 1

An aiforge export is one directory that a network with no access to GitHub
installs aimem, aicrew and aiskills from. It is written on a connected
machine by the `aiforge-export` skill's helper, carried into the closed
network, and read there by each product's installers. This document is the
format's definition; aiskills owns it, and the installers of all three
products follow it.

## Layout

```
<root>/
  MANIFEST            text, UTF-8, LF
  README.txt          the exact install commands for this export; names no github.com
  aimem/<tag>/        the release's assets under their published names, with its SHA256SUMS
  aimem/<tag>/attestations/<asset>.jsonl
                      the asset's attestation bundles, where the release carries its
                      build provenance (see "Build provenance"); no installer reads them
  aicrew/<tag>/       the same
  aiskills/<tag>/     the same
  trusted_root.jsonl  optional: a Sigstore trusted root the export was given
```

**MANIFEST.** Line 1 is `aiforge-export 1`. Each further line is
`<product> <tag>`, for example `aimem v0.10.23`; the products are `aimem`,
`aicrew` and `aiskills`. A reader takes its own product's line and ignores
lines it does not know. A reader that meets a format number it does not
know fails and says so. The format is line-based so that a POSIX shell and
PowerShell read it without a JSON parser. A reader accepts a trailing CR on
a line.

**README.txt** is written by the export script. It gives the commands with
this export's tags filled in, since a closed network has no "latest" to
resolve, and names no github.com URL.

## Rules

1. **The release's own files.** A product directory holds the release's
   assets under their published file names, unchanged, plus the release's
   own `SHA256SUMS`. Every file an installer reads is named in
   `SHA256SUMS`, so a release publishes, as assets covered by its sums,
   every file its installers fetch: the installer scripts themselves, and
   whatever they would otherwise take from the repository (for aimem,
   `install.sh` and the OpenCode plugin). The installers read the same files
   in the connected form and in the closed form; only the base differs.

   A subset of the platform binaries is allowed: an export may hold only the
   platforms its network runs. `SHA256SUMS` is kept whole, so it may list
   files the export does not hold. An installer checks the files it reads
   and treats a listed file it does not need as absent without error.

2. **The base variable names the export root**: an absolute local
   directory, or an `https://` URL. On Windows the directory may also be a
   drive path (`D:\aiforge-export`) or a `\\host\share` path. Plain http is
   refused. An installer reads `<base>/<product>/<tag>/<asset>`. No
   directory listing is assumed: every path follows from MANIFEST, the tag
   and the published asset names, so a static file server is enough.

   | Product | Variable | Note |
   | --- | --- | --- |
   | aimem | `AIMEM_BASE` | |
   | aicrew | `AICREW_BASE` | |
   | aiskills | `AI_SKILLS_EXPORT` | `AI_SKILLS_BASE` already names a Gitea mirror and keeps that meaning; setting both is refused. |

3. **The version.** An explicit version variable wins; without one the tag
   is MANIFEST's. A missing MANIFEST with no explicit version, an unknown
   format number, a missing product line, a missing asset or a sum that
   does not match stops the install with a message naming the path, and
   nothing is installed.

4. **No other host.** With a base given, an installer's own downloads come
   from the base and from no other host, and it prints no github.com URL. A
   command it prints for the next person (the hub installer's member
   one-liner) names the base.

   This rule covers the installer's own downloads. It does not cover what
   the installed product later reads from a source a project's
   configuration names. For example, aimem's hub installer runs `aimem
   process refresh` after an upgrade, which reads each project's configured
   process source; in a closed network that is an internal forge or an
   imported archive. With a base given, such a step ends in a warning when
   its source cannot be reached, and never fails the install or the
   upgrade.

5. **Several tags side by side.** Several releases of one product may sit
   in the same export; MANIFEST names the one a plain install takes.

6. **Integrity is the release's own `SHA256SUMS`**; the export adds no
   signature of its own. The export script checks every file against those
   sums as it downloads it, and each installer checks again what it reads.
   The attestation bundles an export may carry are for a person to check
   with gh; no installer reads them.

## Writing an export

```sh
python3 skills/aiforge-export/aiforge-export --out /media/aiforge-export --platforms linux-amd64,windows-amd64
```

The script takes the latest release of each product unless `--aimem`,
`--aicrew` or `--aiskills` names a tag. For each one it reads the release's
`SHA256SUMS`, downloads every file the sums list (the binaries only for the
chosen platforms), and checks each against the sums as it arrives. It
builds the export beside `--out` and renames it into place only when every
file has been verified, so a failed run leaves no export that looks
complete. The export gets the permissions any new directory and file get
where it is written, so a web server's account or another user can read it.
It refuses an aimem release whose sums list no installer files (releases
before v0.10.23). A release that does not yet publish its installer scripts
as assets is exported anyway, and README.txt says what is missing.

**The forge API's limit and the token.** The script calls the forge's API to
find a latest release and to look up build provenance. Without a token the
API allows 60 calls an hour per address, and a shared address (a hosted CI
runner, a network behind one NAT) can find that spent by others. A 403 or 429
from the API stops the export with a message that names the limit and the
variables below, and nothing is written: an export never silently lacks the
bundles a release has. With `GH_TOKEN` or `GITHUB_TOKEN` set (`GH_TOKEN`
wins), the script sends the token to `https://api.github.com` and to no other
host:

- never to a download host;
- never after a redirect, to any host;
- not to a host given with `--api-base`: an export pointed at another API
  host runs unauthenticated.

The token needs no scope: a read-only token lifts the limit.

## Build provenance

`SHA256SUMS` shows that a file arrived as published. A release whose
workflow attests its files (aimem from v0.10.25) also shows which workflow
built each file, from which commit and tag: a Sigstore-signed SLSA
provenance. Every release also carries GitHub's own release attestation,
which says only that the forge published the bytes; the export goes by the
provenance.

**What the script stores.** It asks the forge's API (with the token, under
the rule above) for the attestations of the release's `SHA256SUMS` digest
(`/repos/<owner>/<product>/attestations/sha256:<hex>`). If one of them is
a build provenance, each exported file that its statement names gets
`<product>/<tag>/attestations/<asset>.jsonl`: the bundles that name that
file, one per line, as `gh attestation download` writes them. A file the
provenance does not name is looked up by its own digest, and an export in
which a file of such a release stays without provenance fails. The bundles
are kept as UTF-8 bytes (a log checkpoint in them is not ASCII). A release
without build provenance is exported as before, and the run and README.txt
say so.

**Checking a file with no network.** On a machine with gh:

```sh
gh attestation verify <dir>/aimem/<tag>/<file> \
  --bundle <dir>/aimem/<tag>/attestations/<file>.jsonl \
  --custom-trusted-root <trusted_root.jsonl> \
  --repo BlackVS/aimem --source-ref refs/tags/<tag> \
  --signer-workflow BlackVS/aimem/.github/workflows/release.yml
```

The command checks the bundle's signature against the trusted root, the
log entry's inclusion proof, the file's digest, the repository, the tag the
run built from and the workflow that signed. It was run with gh 2.102.0
with the network blocked. It passed for every file of an aimem v0.10.25
export, and failed for a file with one byte added, for another tag, and,
in its online form, for want of the network. The check reads the time from
the log entry, not the clock, so the signing certificate's ten minutes do
not limit it.

**The trusted root.** `gh attestation trusted-root` on a connected machine
writes it; `--trusted-root FILE` copies one into the export as
`trusted_root.jsonl`, and README.txt gives its sha256. It must be at least
as new as the release: a root taken before a new log shard cannot check an
entry in that shard, and a newer root checks older bundles. A root that
travels inside the export proves nothing more than `SHA256SUMS` does:
whoever can change the export can change the files, their bundles and the
root together. The root and gh therefore reach the closed network by
another channel, or the copy's sha256 is compared with that of a root taken
independently. The installers do not run this check.

## Installing from an export

Carry the directory into the closed network, or serve it from an internal
https server, then run each product's installer from the export with its
variable set. README.txt in the export has these commands with the tags
filled in:

```sh
# from a directory: run the export's script in place
AIMEM_BASE=/media/aiforge-export bash /media/aiforge-export/aimem/<tag>/boot.sh
AIMEM_BASE=/media/aiforge-export bash /media/aiforge-export/aimem/<tag>/install-hub.sh    # a hub, as root
AI_SKILLS_EXPORT=/media/aiforge-export bash /media/aiforge-export/aiskills/<tag>/boot.sh

# from an https server: pipe the script, as the connected one-liners do
curl -fsSL https://files.example.org/aiforge/aimem/<tag>/boot.sh | AIMEM_BASE=https://files.example.org/aiforge bash
curl -fsSL https://files.example.org/aiforge/aiskills/<tag>/boot.sh | AI_SKILLS_EXPORT=https://files.example.org/aiforge bash
```

On Windows a directory export's `boot.ps1` is copied to a local folder
and the copy runs with `powershell -File`, the copy inside the `try` with
`-ErrorAction Stop` and the copy and the variable removed in its `finally`
(Windows PowerShell refuses an unsigned script read straight from most
shares under `RemoteSigned`, and a failed copy must not run a copy an
interrupted run left behind). An https export's runs with
`irm <url>/<product>/<tag>/boot.ps1 | iex`, the base variable set first. aimem's and aiskills' installers document and test the
https form; aicrew's do not yet, so README.txt gives aicrew only the
directory form.

## Process sets

From v0.10.25, aimem's release publishes its reference process set as
`aimem-process.tar.gz`, listed in its `SHA256SUMS`, so the export carries
it under `aimem/<tag>/`. README.txt then names the file and gives both
commands, which need neither git nor the network:

```sh
aimem process select <repository> <tag> --from-archive <dir>/aimem/<tag>/aimem-process.tar.gz \
  --sums <dir>/aimem/<tag>/SHA256SUMS -p <project>
aimem process default set <repository> <tag> --from-archive <dir>/aimem/<tag>/aimem-process.tar.gz \
  --sums <dir>/aimem/<tag>/SHA256SUMS
```

For an earlier aimem release, a closed network imports a process set from
an archive made with `aimem process export` on a connected machine, and
README.txt gives those commands.
