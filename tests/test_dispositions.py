"""Behavioral tests for the disposition model (references/dispositions.md).

The skill is prose executed by a model, so these tests pin the *reference
implementation* of the rules that any automation around the skill must follow:
verdict as a pure function of dispositions, BLOCKER gating, conservative
duplicate resolution, forge review-state mapping, exact-head semantics, and the
rendered output shape. If the reference text changes, change these together.
Run: python3 -m unittest tests/test_dispositions.py
"""
import json, re, unittest, os

DISPOSITIONS = ("BLOCKER", "CURRENT_SCOPE_IMPROVEMENT", "FOLLOW_UP", "OBSERVATION", "REJECTED", "RISK_ACCEPTED")
SEVERITIES = ("critical", "high", "medium", "low")
ORDER = {d: i for i, d in enumerate(("REJECTED", "OBSERVATION", "RISK_ACCEPTED", "FOLLOW_UP", "CURRENT_SCOPE_IMPROVEMENT", "BLOCKER"))}
SECTIONS = ("BLOCKERS", "CURRENT-SCOPE IMPROVEMENTS", "FOLLOW-UPS", "OBSERVATIONS", "REJECTED", "RISK ACCEPTED", "VERDICT", "RISK")


CONF = ("confirmed", "confirmed-runtime", "plausible", "plausible-runtime", "rejected-runtime")
RUNTIME = ("confirmed-runtime", "plausible-runtime", "rejected-runtime")


def validate(f):
    """A finding is valid only if its disposition is a known enum and the fields the disposition and confidence require are present."""
    assert f["disposition"] in DISPOSITIONS, f["disposition"]
    assert f["severity"] in SEVERITIES
    assert f["confidence"] in CONF, f["confidence"]
    if f["confidence"] in RUNTIME:
        assert f.get("assumption") and f.get("reproducer"), "runtime-dependent finding needs assumption and reproducer"
    if f["confidence"] == "rejected-runtime":
        assert f["disposition"] == "REJECTED", "a disproved runtime assumption is REJECTED"
    if f["disposition"] == "BLOCKER":
        assert f.get("failure_path"), "BLOCKER needs a concrete failure path"
        assert f.get("acceptance_criterion"), "BLOCKER needs a named acceptance criterion"
        if f["confidence"] == "plausible-runtime":
            assert f.get("unverified_is_criteria_failure") is True, "PLAUSIBLE-RUNTIME cannot be BLOCKER unless leaving it unverified is itself a criteria failure"
        else:
            assert f["confidence"] in ("confirmed", "confirmed-runtime"), "only a confirmed finding can be a BLOCKER"
    if f["disposition"] == "FOLLOW_UP":
        assert f.get("boundary") and f.get("first_increment"), "FOLLOW_UP needs boundary and first increment"
    return f


def verdict(findings, ran=True):
    if not ran:
        return "REVIEW_COULD_NOT_RUN"
    return "RETURN_TO_IMPLEMENTATION" if any(f["disposition"] == "BLOCKER" for f in findings) else "READY_FOR_HUMAN_MERGE"


def forge_state(findings, v):
    if v == "RETURN_TO_IMPLEMENTATION":
        return "REQUEST_CHANGES"
    return "APPROVE" if not findings else "COMMENT"


def consolidate(a, b):
    """Two findings about one root cause: keep the less blocking disposition unless the merged evidence passes the BLOCKER test."""
    merged = dict(max((a, b), key=lambda f: len(f.get("failure_path") or "")))
    lo = min((a, b), key=lambda f: ORDER[f["disposition"]])
    merged["disposition"] = lo["disposition"]
    both_blocker_ok = all(f.get("failure_path") and f.get("acceptance_criterion") and f["confidence"] == "confirmed" for f in (a, b))
    if "BLOCKER" in (a["disposition"], b["disposition"]) and both_blocker_ok:
        merged["disposition"] = "BLOCKER"
    return merged


def needs_rereview(reviewed_sha, event):
    return event.get("type") == "source_changed" and event.get("sha") != reviewed_sha


def render(bot, sha, objective, findings, risk, ran=True):
    groups = {d: [] for d in DISPOSITIONS}
    for f in findings:
        groups[f["disposition"]].append(f)
    def bullets(d, extra=()):
        if not groups[d]:
            return ["- None"]
        out = []
        for f in groups[d]:
            tag = "[%s] [%s] %s" % (f["disposition"], f["confidence"].upper(), f["summary"])
            if f.get("file"):
                tag += " (%s:%s)" % (f["file"], f.get("line"))
            out.append("- " + tag)
            if f["confidence"] in RUNTIME:
                out.append("  Assumption: %s" % f["assumption"]); out.append("  Reproducer: %s" % f["reproducer"])
                if f.get("current_evidence"): out.append("  Current evidence: %s" % f["current_evidence"])
            for k, label in extra:
                if f.get(k):
                    out.append("  %s: %s" % (label, f[k]))
        return out
    v = verdict(findings, ran)
    lines = ["[%s review] reviewed at head %s" % (bot, sha), "", "CURRENT OBJECTIVE", objective, "",
             "BLOCKERS", *bullets("BLOCKER", (("failure_path", "Failure path"), ("acceptance_criterion", "Violated criterion"))), "",
             "CURRENT-SCOPE IMPROVEMENTS", *bullets("CURRENT_SCOPE_IMPROVEMENT"), "",
             "FOLLOW-UPS", *bullets("FOLLOW_UP", (("failure_path", "Failure path"), ("boundary", "Boundary"), ("first_increment", "First increment"))), "",
             "OBSERVATIONS", *bullets("OBSERVATION"), "", "REJECTED", *bullets("REJECTED"), "", "RISK ACCEPTED", *bullets("RISK_ACCEPTED"), "",
             "VERDICT", v + ("" if v != "READY_FOR_HUMAN_MERGE" or not findings else " — follow-ups and observations do not block"), "",
             "RISK", "%s — %s" % (risk[0], risk[1])]
    return "\n".join(lines)


def F(disposition, severity="medium", confidence="confirmed", **kw):
    base = {"file": "a.py", "line": 1, "severity": severity, "disposition": disposition, "summary": "s", "failure_path": None,
            "acceptance_criterion": None, "boundary": None, "first_increment": None, "confidence": confidence,
            "assumption": None, "reproducer": None, "current_evidence": None, "unverified_is_criteria_failure": False}
    base.update(kw)
    return base


BLOCK = dict(failure_path="input X -> crash", acceptance_criterion="AC1: must not crash on X")
FUP = dict(boundary="storage layer", first_increment="add a unit test for the retry path")


class VerdictTests(unittest.TestCase):
    def test_no_findings_is_ready(self):
        self.assertEqual(verdict([]), "READY_FOR_HUMAN_MERGE"); self.assertEqual(forge_state([], verdict([])), "APPROVE")
    def test_only_follow_ups_is_ready(self):
        fs = [validate(F("FOLLOW_UP", "high", **FUP))]; self.assertEqual(verdict(fs), "READY_FOR_HUMAN_MERGE"); self.assertEqual(forge_state(fs, verdict(fs)), "COMMENT")
    def test_only_observations_is_ready(self):
        self.assertEqual(verdict([validate(F("OBSERVATION"))]), "READY_FOR_HUMAN_MERGE")
    def test_only_current_scope_improvements_is_ready(self):
        self.assertEqual(verdict([validate(F("CURRENT_SCOPE_IMPROVEMENT"))]), "READY_FOR_HUMAN_MERGE")
    def test_one_confirmed_blocker_returns_to_implementation(self):
        fs = [validate(F("BLOCKER", **BLOCK)), validate(F("FOLLOW_UP", **FUP))]
        self.assertEqual(verdict(fs), "RETURN_TO_IMPLEMENTATION"); self.assertEqual(forge_state(fs, verdict(fs)), "REQUEST_CHANGES")
    def test_high_severity_follow_up_never_blocks(self):
        fs = [validate(F("FOLLOW_UP", "critical", failure_path="spoofed host accepts password", **FUP))]
        self.assertEqual(verdict(fs), "READY_FOR_HUMAN_MERGE"); self.assertNotEqual(forge_state(fs, verdict(fs)), "REQUEST_CHANGES")
    def test_high_risk_rating_does_not_change_verdict(self):
        fs = [validate(F("OBSERVATION", "high"))]; self.assertEqual(verdict(fs), "READY_FOR_HUMAN_MERGE"); self.assertIn("HIGH", render("bot", "abc", "o", fs, ("HIGH", "privileged path")))
    def test_could_not_run(self):
        self.assertEqual(verdict([], ran=False), "REVIEW_COULD_NOT_RUN")


class BlockerGatingTests(unittest.TestCase):
    def test_speculative_finding_cannot_be_blocker(self):
        with self.assertRaises(AssertionError): validate(F("BLOCKER", "critical", acceptance_criterion="AC1"))          # no failure path
        with self.assertRaises(AssertionError): validate(F("BLOCKER", "critical", failure_path="x -> y"))                 # no named criterion
        with self.assertRaises(AssertionError): validate(F("BLOCKER", "critical", confidence="plausible", **BLOCK))     # not confirmed
    def test_follow_up_requires_boundary_and_first_increment(self):
        with self.assertRaises(AssertionError): validate(F("FOLLOW_UP", boundary="x"))
        f = validate(F("FOLLOW_UP", **FUP)); r = render("bot", "abc", "o", [f], ("LOW", "ok"))
        self.assertIn("Boundary: storage layer", r); self.assertIn("First increment: add a unit test", r)
    def test_disposition_is_an_enum_not_prose(self):
        with self.assertRaises(AssertionError): validate(F("blocker-ish", **BLOCK))
        with self.assertRaises(AssertionError): validate(F("Needs rework", **BLOCK))
    def test_severity_and_disposition_are_independent(self):
        low_blocker = validate(F("BLOCKER", "medium", **BLOCK)); high_fup = validate(F("FOLLOW_UP", "critical", **FUP))
        self.assertEqual(verdict([low_blocker]), "RETURN_TO_IMPLEMENTATION"); self.assertEqual(verdict([high_fup]), "READY_FOR_HUMAN_MERGE")


RT = dict(assumption="tester:2 runs as non-root on routed Docker agents", reproducer="credential-free tester:2 workflow printing os.geteuid() and ownership of a new 0600 file")


class RuntimeDependentTests(unittest.TestCase):
    def test_runtime_assumption_cannot_be_blocker_on_source_evidence_alone(self):
        with self.assertRaises(AssertionError): validate(F("BLOCKER", "high", confidence="plausible-runtime", **BLOCK, **RT))
        f = validate(F("OBSERVATION", "high", confidence="plausible-runtime", failure_path="x -> y", **RT))
        self.assertEqual(verdict([f]), "READY_FOR_HUMAN_MERGE"); self.assertEqual(forge_state([f], verdict([f])), "COMMENT")
    def test_runtime_finding_requires_assumption_and_reproducer(self):
        with self.assertRaises(AssertionError): validate(F("OBSERVATION", confidence="plausible-runtime"))
        with self.assertRaises(AssertionError): validate(F("OBSERVATION", confidence="plausible-runtime", assumption="a"))
    def test_reproduced_failure_may_block(self):
        f = validate(F("BLOCKER", "high", confidence="confirmed-runtime", **BLOCK, **RT)); self.assertEqual(verdict([f]), "RETURN_TO_IMPLEMENTATION")
    def test_unverified_is_itself_a_criteria_failure_may_block(self):
        f = validate(F("BLOCKER", "high", confidence="plausible-runtime", unverified_is_criteria_failure=True, **BLOCK, **RT)); self.assertEqual(verdict([f]), "RETURN_TO_IMPLEMENTATION")
    def test_disproved_assumption_is_rejected_and_recorded(self):
        with self.assertRaises(AssertionError): validate(F("OBSERVATION", confidence="rejected-runtime", **RT))
        f = validate(F("REJECTED", confidence="rejected-runtime", current_evidence="probe 2026-09-14: euid=0 on all Docker agents", **RT))
        r = render("bot", "abc", "o", [f], ("LOW", "x")); self.assertIn("[REJECTED] [REJECTED-RUNTIME]", r); self.assertIn("Reproducer:", r); self.assertIn("Current evidence: probe", r)
    def test_render_carries_assumption_and_reproducer(self):
        r = render("bot", "abc", "o", [validate(F("OBSERVATION", confidence="plausible-runtime", **RT))], ("LOW", "x"))
        self.assertIn("Assumption: tester:2", r); self.assertIn("Reproducer: credential-free", r)


class ConsolidationTests(unittest.TestCase):
    def test_duplicates_resolve_conservatively(self):
        a = F("BLOCKER", "high", failure_path="x -> y", acceptance_criterion=None, confidence="plausible")   # would not pass validate; models the raw sub-agent claim
        b = validate(F("FOLLOW_UP", "high", failure_path="x -> y", **FUP))
        m = consolidate(a, b); self.assertEqual(m["disposition"], "FOLLOW_UP")
    def test_duplicates_both_passing_blocker_test_stay_blocker(self):
        a = validate(F("BLOCKER", **BLOCK)); b = validate(F("BLOCKER", **BLOCK)); self.assertEqual(consolidate(a, b)["disposition"], "BLOCKER")
    def test_specialist_disposition_survives_consolidation(self):
        sec = validate(F("FOLLOW_UP", "critical", summary="security specialist", **FUP)); file_rev = validate(F("OBSERVATION", "low", summary="file reviewer"))
        self.assertEqual(consolidate(sec, file_rev)["disposition"], "OBSERVATION")     # never escalates
        self.assertEqual(verdict([sec, file_rev]), "READY_FOR_HUMAN_MERGE")


class ExactHeadTests(unittest.TestCase):
    def test_metadata_only_update_needs_no_rereview(self):
        for ev in ({"type": "backlog_item_added"}, {"type": "disposition_discussed"}, {"type": "finding_rejected_with_evidence"}, {"type": "risk_accepted_by_owner"}, {"type": "pr_metadata_edited"}):
            self.assertFalse(needs_rereview("abc123", ev))
    def test_changed_source_sha_needs_rereview(self):
        self.assertTrue(needs_rereview("abc123", {"type": "source_changed", "sha": "def456"})); self.assertFalse(needs_rereview("abc123", {"type": "source_changed", "sha": "abc123"}))
    def test_risk_acceptance_is_recorded_without_changing_sha(self):
        fs = [validate(F("RISK_ACCEPTED", "high", summary="reusable test password", failure_path="repo read -> password"))]
        r = render("bot", "abc123", "o", fs, ("HIGH", "accepted")); self.assertIn("reviewed at head abc123", r); self.assertIn("[RISK_ACCEPTED]", r); self.assertEqual(verdict(fs), "READY_FOR_HUMAN_MERGE")


class RenderTests(unittest.TestCase):
    def test_all_sections_always_present_and_parseable(self):
        for fs in ([], [validate(F("BLOCKER", **BLOCK))], [validate(F("FOLLOW_UP", **FUP)), validate(F("OBSERVATION"))]):
            r = render("review-bot", "ee5b3a1", "Rename three workflows", fs, ("LOW", "naming only"))
            self.assertTrue(r.startswith("[review-bot review] reviewed at head ee5b3a1"))
            for sec in SECTIONS: self.assertRegex(r, r"(?m)^%s$" % re.escape(sec))
            empty = [sec for sec in SECTIONS[:6] if re.search(r"(?m)^%s\n- None$" % re.escape(sec), r)]
            self.assertEqual(len(empty), 6 - len({f["disposition"] for f in fs}))
    def test_ready_with_follow_ups_says_they_do_not_block(self):
        r = render("bot", "abc", "o", [validate(F("FOLLOW_UP", **FUP))], ("LOW", "x")); self.assertIn("READY_FOR_HUMAN_MERGE — follow-ups and observations do not block", r)
    def test_reference_doc_and_skill_agree_on_enums(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ref = open(os.path.join(here, "skills/oh-code-review/references/dispositions.md")).read(); fan = open(os.path.join(here, "skills/oh-code-review/references/fan-out.md")).read()
        for d in DISPOSITIONS: self.assertIn(d, ref); self.assertIn(d, fan)
        for c in ("confirmed-runtime", "plausible-runtime", "rejected-runtime", "unverified_is_criteria_failure"): self.assertIn(c, ref); self.assertIn(c, fan)
        for v in ("READY_FOR_HUMAN_MERGE", "RETURN_TO_IMPLEMENTATION", "REVIEW_COULD_NOT_RUN"): self.assertIn(v, ref)
        for sec in SECTIONS: self.assertIn(sec, ref)


if __name__ == "__main__":
    unittest.main()
