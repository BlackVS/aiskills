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
  aicrew/<tag>/       the same
  aiskills/<tag>/     the same
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
complete. It refuses an aimem release whose sums list no installer files
(releases before v0.10.23). A release that does not yet publish its
installer scripts as assets is exported anyway, and README.txt says what is
missing.

## Installing from an export

Carry the directory into the closed network, or serve it from an internal
https server, then run each product's installer from the export with its
variable set. README.txt in the export has these commands with the tags
filled in:

```sh
AIMEM_BASE=/media/aiforge-export bash /media/aiforge-export/aimem/<tag>/boot.sh
AIMEM_BASE=/media/aiforge-export bash /media/aiforge-export/aimem/<tag>/install-hub.sh    # a hub, as root
AI_SKILLS_EXPORT=/media/aiforge-export bash /media/aiforge-export/aiskills/<tag>/boot.sh
```

## Process sets

aimem's release does not yet publish its reference process set. Until it
does, a closed network imports a process set from an archive made with
`aimem process export` on a connected machine; README.txt in the export
gives the commands.
