## What

<!-- One paragraph: what changed and why. -->

## Evidence

<!-- Command + observed result, never intentions: the test summary lines, or
     the exact commands that exercised the change end to end. -->

## Checklist

- [ ] The three checks from AGENTS.md pass (installers/boot/changelog/skills, review-hook runtime, `render.py --check`)
- [ ] Pre-push review gate passed (`oh-code-review` at medium; findings fixed or explicitly waived)
- [ ] `VERSION` bumped and a `CHANGELOG.md` entry added (row in `consumers/openhands-review-hook/UPDATING.md` if a hands site must act)
- [ ] Both installers changed together (`install.sh` and `install.ps1`, `boot.sh` and `boot.ps1`)
- [ ] Nothing private: no hostnames, tokens, credential paths, private names or session URLs; this repository is public
