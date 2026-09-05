"""Quality scoring math, fail-closed statuses, judge resolution, fingerprint
binding, and the calibration gate's refusal to trust a bare document."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assay import quality
from assay.quality import (
    DEFAULT_GATES,
    JudgeSeat,
    build_report,
    calibration_check,
    evidence_digest,
    quality_fingerprint,
    resolve_single_judge,
    upsert_quality_block,
)


MANIFEST = quality.MANIFEST_PATH
ITEMS = quality.load_quality_items(MANIFEST)
PROMPT = quality.prompt_digest(MANIFEST)
INSTRUCTION = "Fix the bug described in the issue."
PATCH = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)
SEAT = JudgeSeat(
    alias="gpt-5.6-sol",
    model="gpt-5.6-sol",
    model_id="openai/responses/gpt-5.6-sol",
    endpoint="http://127.0.0.1:8766/responses",
)


def _fp(instruction=INSTRUCTION, patch=PATCH, model_id=SEAT.model_id, prompt=PROMPT):
    packet, _ = quality.build_quality_packet(instruction, patch)
    return quality_fingerprint(
        judge_model=model_id, evidence=evidence_digest(packet), prompt=prompt
    )


def _completion(yes: bool, cite: bool = True, trunc: bool = False) -> str:
    ev = "[[EVIDENCE: a.py hunk 1]]" if cite else ""
    return (
        "[[RATIONALE: because]]\n"
        f"[[SATISFIED: {'Yes' if yes else 'No'}]]\n"
        f"[[TRUNCATION_AFFECTED: {'Yes' if trunc else 'No'}]]\n" + ev
    )


def _records(answers, member=SEAT.alias, fp=None, **kw):
    fp = fp or _fp()
    return [
        {
            "member": member,
            "model": SEAT.model,
            "item_id": it.id,
            "completion": _completion(a, **kw),
            "error": "",
            quality.FINGERPRINT_KEY: fp,
        }
        for it, a in zip(ITEMS, answers)
    ]


def _report(records, patch=PATCH, seat=SEAT, calibrated=False, instruction=INSTRUCTION):
    sha = hashlib.sha256(patch.encode()).hexdigest() if patch is not None else None
    return build_report(
        task_uuid="u",
        model="opus-5",
        run_id="run_1",
        manifest_path=MANIFEST,
        instruction=instruction,
        patch=patch,
        patch_sha256=sha,
        records=records,
        seat=seat,
        calibrated=calibrated,
    )


def test_all_yes_scores_one_and_all_no_scores_zero():
    assert _report(_records([True] * 7)).score == 1.0
    assert _report(_records([False] * 7)).score == 0.0


def test_score_is_weighted_pass_ratio():
    rep = _report(_records([True] * 5 + [False] * 2))
    assert rep.status == "scored"
    assert rep.score == round(5 / 7, 4)
    d = rep.to_dict()
    assert d["counts"] == {"total": 7, "passed": 5, "failed": 2, "undecided": 0}
    assert d["version"] == {
        "quality": quality.QUALITY_VERSION,
        "prompt_digest": PROMPT,
        "manifest_digest": quality.manifest_digest(MANIFEST),
    }
    assert d["judge"] == {"member": SEAT.alias, "model": SEAT.model_id}
    assert d["evidence_digest"] == rep.evidence and len(rep.evidence) == 16


def test_any_abstention_means_no_score():
    recs = _records([True] * 7)
    recs[3]["completion"] = _completion(True, cite=False)  # uncited -> abstain
    rep = _report(recs)
    assert rep.status == "unjudged"
    assert rep.score is None
    assert "Q4=abstain_uncited" in rep.reasons[0]


def test_missing_item_means_no_score():
    rep = _report(_records([True] * 7)[:-1])
    assert rep.status == "unjudged" and rep.score is None
    assert "Q7=abstain_no_verdict" in rep.reasons[0]


def test_truncated_verdicts_abstain():
    assert _report(_records([True] * 7, trunc=True)).status == "unjudged"


def test_missing_and_empty_patch_statuses():
    assert _report([], patch=None).status == "evidence_missing"
    assert _report([], patch="   \n").status == "empty_patch"
    assert _report([], patch=None).score is None


def test_no_records_is_unjudged():
    rep = _report([])
    assert rep.status == "unjudged"
    assert rep.reasons == ["no quality verdicts recorded for this run"]


def test_foreign_member_refused():
    rep = _report(_records([True] * 7, member="sonnet-5"))
    assert rep.status == "unjudged"
    assert "sonnet-5" in rep.reasons[0] and SEAT.alias in rep.reasons[0]


def test_stale_fingerprint_refused():
    rep = _report(_records([True] * 7, fp="0000000000000000"))
    assert rep.status == "unjudged"
    assert "fingerprint" in rep.reasons[0]


def test_changed_instruction_invalidates_verdicts():
    """The judge saw the instruction, so it is part of the evidence digest."""
    recs = _records([True] * 7)  # fingerprinted against INSTRUCTION
    assert _report(recs).status == "scored"
    rep = _report(recs, instruction=INSTRUCTION + " And also this.")
    assert rep.status == "unjudged" and "fingerprint" in rep.reasons[0]


def test_fingerprint_moves_with_judge_patch_prompt_and_clipping():
    base = _fp()
    assert _fp(model_id="b") != base
    assert _fp(patch=PATCH + "+# x\n") != base
    assert _fp(prompt="p") != base
    assert _fp() == base
    long_patch = PATCH + "x" * 100
    p1, t1 = quality.build_quality_packet(INSTRUCTION, long_patch, patch_cap=50)
    p2, t2 = quality.build_quality_packet(INSTRUCTION, long_patch, patch_cap=60)
    assert t1 and t2 and evidence_digest(p1) != evidence_digest(p2)


def test_resolve_single_judge_matches_shell_derivation(tmp_path: Path):
    p = tmp_path / "j.json"
    p.write_text(json.dumps({"judge_model": "openai/responses/gpt-5.6-sol"}))
    s = resolve_single_judge(p)
    assert (s.alias, s.model, s.endpoint) == (
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        "http://127.0.0.1:8766/responses",
    )
    p.write_text(json.dumps({"judge_model": "anthropic/claude-sonnet-5"}))
    s = resolve_single_judge(p)
    assert (s.alias, s.model, s.endpoint) == (
        "sonnet-5",
        "claude-sonnet-5",
        "http://127.0.0.1:8765/v1/messages",
    )
    p.write_text(json.dumps({"model": "claude-opus-5"}))
    assert resolve_single_judge(p, "http://x/y").endpoint == "http://x/y"
    p.write_text(json.dumps({}))
    with pytest.raises(ValueError, match="judge_model"):
        resolve_single_judge(p)


def test_packet_has_instruction_and_diff_and_no_truth():
    text, truncated = quality.build_quality_packet("Fix the bug", PATCH)
    assert "Fix the bug" in text and PATCH in text and not truncated
    assert "Reference account" not in text
    text, truncated = quality.build_quality_packet("i", "✓" * 50, patch_cap=10)
    assert truncated and "cut at 10 bytes" in text


def good_calibration(model_id=SEAT.model_id, prompt=PROMPT, manifest=None, **over):
    doc = {
        "quality_version": quality.QUALITY_VERSION,
        "model": model_id,
        "prompt_digest": prompt,
        "manifest_digest": manifest or quality.manifest_digest(MANIFEST),
        "passed": True,
        "n_subjects": 50,
        "n_dev": 20,
        "n_holdout": 30,
        "n_raters": 2,
        "inter_rater_weighted_kappa": 0.7,
        "holdout_spearman": 0.7,
        "holdout_pearson": 0.7,
        "per_dimension": {
            d: {"balanced_accuracy": 0.8, "n": 30, "both_classes": True}
            for d in quality.QUALITY_DIMENSIONS
        },
        "gates": dict(DEFAULT_GATES),
    }
    doc.update(over)
    return doc


def test_calibration_gate_refuses_bare_or_weak_documents(tmp_path: Path):
    p = tmp_path / "judge_calibration.json"
    kw = dict(
        judge_model=SEAT.model_id,
        prompt=PROMPT,
        manifest=quality.manifest_digest(MANIFEST),
    )
    ok, why = calibration_check(p, **kw)
    assert not ok and "no judge_calibration.json" in why

    # the four-field form that used to be enough
    p.write_text(
        json.dumps(
            {
                "quality_version": quality.QUALITY_VERSION,
                "model": SEAT.model_id,
                "prompt_digest": PROMPT,
                "passed": True,
            }
        )
    )
    ok, why = calibration_check(p, **kw)
    assert not ok and "lacks" in why

    p.write_text(json.dumps(good_calibration()))
    assert calibration_check(p, **kw) == (True, "calibrated")

    bad = [
        ("passed", False),
        ("model", "other"),
        ("prompt_digest", "x"),
        ("manifest_digest", "x"),
        ("quality_version", "v0"),
        ("n_subjects", 49),
        ("n_dev", 19),
        ("n_holdout", 29),
        ("n_raters", 1),
        ("inter_rater_weighted_kappa", 0.59),
        ("holdout_spearman", 0.59),
        ("holdout_pearson", None),
        ("stale_subjects", ["u/gold"]),
        ("gates", {**DEFAULT_GATES, "min_spearman": 0.1}),  # looser gates do not help
    ]
    for k, v in bad:
        doc = good_calibration(**{k: v})
        if k == "gates":
            doc["holdout_spearman"] = 0.2
        p.write_text(json.dumps(doc))
        assert not calibration_check(p, **kw)[0], k

    # a dimension without both classes is reported, not gated
    doc = good_calibration()
    doc["per_dimension"]["naming"] = {
        "balanced_accuracy": None,
        "n": 30,
        "both_classes": False,
    }
    p.write_text(json.dumps(doc))
    assert calibration_check(p, **kw)[0]


def test_md_block_upsert_is_idempotent():
    rep = _report(_records([True] * 6 + [False]))
    block = rep.render_md()
    md = "# Score\n\n| a | b |\n"
    once = upsert_quality_block(md, block)
    twice = upsert_quality_block(once, block)
    assert once == twice
    assert once.count(quality.QUALITY_MD_START) == 1
    assert once.startswith(md.rstrip("\n"))
    assert "score_quality" in once and "0.8571" in once


def test_correlations_and_kappa():
    assert quality.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert quality.spearman([1, 2, 3], [10, 30, 20]) == pytest.approx(0.5)
    assert quality.spearman([1, 1, 1], [1, 2, 3]) is None
    assert quality.weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(
        1.0
    )
    assert quality.weighted_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) < 0
    assert quality.balanced_accuracy(
        [True, False, True, False], [True, False, False, True]
    ) == pytest.approx(0.5)
    assert quality.balanced_accuracy([True, True], [True, True]) is None
