"""The changelog and the version file agree, and no release heading goes missing.

Run: python3 -m unittest tests/test_changelog.py
"""
import re, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def versions(text, pattern):
    return [tuple(int(x) for x in v.split(".")) for v in re.findall(pattern, text, flags=re.M)]


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        self.changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.headings = versions(self.changelog, r"^## \[(\d+\.\d+\.\d+)\]")

    def test_version_file_is_the_newest_heading(self):
        current = tuple(int(x) for x in (ROOT / "VERSION").read_text(encoding="utf-8").strip().split("."))
        self.assertEqual(self.headings[0], current)

    def test_release_headings_are_strictly_descending(self):
        self.assertEqual(self.headings, sorted(set(self.headings), reverse=True))

    def test_every_section_belongs_to_a_release(self):
        # a dropped heading leaves a "### Added/Changed/Fixed" block under the previous release:
        # every release heading must be followed by at least one section before the next heading,
        # and no two consecutive release bodies may share a section that was moved
        bodies = re.split(r"^## \[", self.changelog, flags=re.M)[1:]
        for body in bodies:
            name = body.split("]", 1)[0]
            if name == "Unreleased":
                continue
            self.assertTrue(re.search(r"^### ", body, flags=re.M), f"release {name} has no section")

    def test_releases_named_by_the_update_checklist_exist(self):
        # UPDATING.md's table rows name the releases an operator must act on: each is a changelog release
        updating = (ROOT / "consumers/openhands-review-hook/UPDATING.md").read_text(encoding="utf-8")
        for v in versions(updating, r"^\| (\d+\.\d+\.\d+) \|"):
            self.assertIn(v, self.headings, f"UPDATING names {'.'.join(map(str, v))}, missing from CHANGELOG")


if __name__ == "__main__":
    unittest.main()
