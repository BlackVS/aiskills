"""Every shipped skill's frontmatter satisfies the compatibility rules in README:
strict YAML (Codex drops a skill it cannot parse), tool-neutral name and
description limits, name equal to the directory.

Run: python3 -m unittest tests/test_skill_frontmatter.py
"""
import pathlib, re, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter(text):
    assert text.startswith("---\n"), "frontmatter must open the file"
    end = text.index("\n---", 4)
    return text[4:end]


def scalar(fm, key):
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    return m.group(1) if m else None


def yaml_safe(value):
    """A plain scalar may not contain ': ' or ' #'; a quoted one must close its quote."""
    if value.startswith("'"):
        return value.endswith("'") and "'" not in value[1:-1].replace("''", "")
    if value.startswith('"'):
        return value.endswith('"') and '"' not in value[1:-1].replace('\\"', "")
    return ": " not in value and " #" not in value and not value.endswith(":")


class SkillFrontmatter(unittest.TestCase):
    def test_every_skill(self):
        skills = sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())
        self.assertTrue(skills)
        for d in skills:
            with self.subTest(skill=d.name):
                fm = frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
                name, desc = scalar(fm, "name"), scalar(fm, "description")
                self.assertEqual(name, d.name, "name equals the directory")
                self.assertTrue(NAME.match(name), name)
                self.assertTrue(desc, "description present")
                self.assertTrue(yaml_safe(desc), "description is strict YAML (quote it when it contains ': ')")
                self.assertLessEqual(len(desc.strip("'\"")), 1024)

    def test_yaml_safe_rule(self):
        self.assertFalse(yaml_safe("Review the design: goals, risk"))
        self.assertTrue(yaml_safe("'Review the design: goals, \"risk\"'"))
        self.assertTrue(yaml_safe("Rigorous review (BLOCKER / FOLLOW_UP)"))
        self.assertFalse(yaml_safe("'unterminated"))


if __name__ == "__main__":
    unittest.main()
