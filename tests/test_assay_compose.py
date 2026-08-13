"""Pins for the single-judge composition (remove kappa, keep alpha).

The process score is the plain average (det + rubric) / 2 — no weight constant,
no dial, nothing derived from inter-judge agreement (which does not exist with
one judge). Alpha and the stratified score_rl form are untouched from the
pre-change scorer.
"""

from __future__ import annotations

from assay.compose import (
    alpha_for_task,
    combine,
    eval_score,
    process_score,
    rl_score,
)
from assay.writeback import assay_block


def test_no_weight_dial_exists():
    import assay.compose as compose

    assert not hasattr(compose, "RUBRIC_WEIGHT")
    assert not hasattr(compose, "channel_weight")


def test_process_is_simple_average_of_the_two_channels():
    # spec §3.4 worked example: (0.2195 + 0.2349) / 2 = 0.2272
    assert round(process_score(det=0.2195, rubric=0.2349), 4) == 0.2272


def test_missing_rubric_leaves_process_on_deterministic_alone():
    assert process_score(det=0.9, rubric=None) == 0.9


def test_agreement_module_is_gone():
    import importlib.util

    assert importlib.util.find_spec("assay.agreement") is None


def test_eval_score_formula_and_gate():
    # gate multiplies the whole blend, so a voided run scores exactly zero
    kw = dict(outcome=1.0, det=0.8, rubric=0.6, alpha=0.05)
    assert eval_score(**kw, gate=0) == 0.0
    p = process_score(det=0.8, rubric=0.6)
    assert eval_score(**kw, gate=1) == combine(outcome=1.0, p=p, alpha=0.05, gate=1)


def test_rl_score_stratifies_process_only():
    # a lone run in its stratum gets the 0.5 fallback, outcome side unchanged
    got = rl_score(
        outcome=1.0,
        det=0.8,
        rubric=0.6,
        alpha=0.05,
        gate=1,
        stratum_processes=[process_score(det=0.8, rubric=0.6)],
    )
    assert got == combine(outcome=1.0, p=0.5, alpha=0.05, gate=1)


def test_alpha_derivation_untouched():
    # alpha still comes from the task's target count with the 0.05 cap
    assert alpha_for_task(4) == min(0.05, 0.25 / (2 * 1.25))
    assert alpha_for_task(100) == min(0.05, 0.02 / (2 * 1.02))


def test_assay_block_names_the_judge_not_a_council():
    report = {
        "composition": {"alpha": 0.05, "stratum_size": 3},
        "process": {"gate": 1},
        "judge": {"model": "sonnet-5"},
        "status": "scored",
    }
    block = assay_block(report)
    assert list(block) == [
        "alpha",
        "gate",
        "stratum_size",
        "judge",
        "status",
    ]
    assert block["judge"] == "sonnet-5"
    assert "kappa" not in block and "council" not in block
    assert "rubric_weight" not in block
