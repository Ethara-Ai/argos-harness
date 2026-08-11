"""Publish the verifier's rewards into a run's result.json.

The harness owns ``score``, ``score_binary`` and ``score_continuous_v2``; those
are read, never rewritten. Everything this module adds is additive and follows
the same ``score_*`` naming, so a consumer reading the block does not have to
know which producer wrote which field.
"""

from __future__ import annotations

from typing import Any


ASSAY_SCORE_FIELDS = (
    "score_outcome",
    "score_deterministic",
    "score_rubric",
    "score_process",
    "score_eval",
    "score_rl",
)

HARNESS_OWNED = ("score", "score_binary", "score_continuous_v2")


def assay_scores(report: dict[str, Any]) -> dict[str, float]:
    """The verifier's numbers for one run, or {} when the run is not scoreable."""
    proc = report.get("process") or {}
    comp = report.get("composition") or {}
    if not comp or proc.get("score") is None:
        return {}
    return {
        "score_outcome": (report.get("outcome") or {}).get("score"),
        "score_deterministic": proc.get("deterministic"),
        "score_rubric": proc.get("rubric"),
        "score_process": proc.get("score"),
        "score_eval": comp.get("score_eval"),
        "score_rl": comp.get("score_rl"),
    }


def merge_scores(scores: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    added = assay_scores(report)
    if not added:
        return scores
    for k in HARNESS_OWNED:
        added.pop(k, None)
    scores.update({k: v for k, v in added.items() if v is not None})
    return scores


def assay_block(report: dict[str, Any]) -> dict[str, Any]:
    """Metadata without which the numbers above cannot be interpreted."""
    comp = report.get("composition") or {}
    proc = report.get("process") or {}
    return {
        "alpha": comp.get("alpha"),
        "rubric_weight": comp.get("rubric_weight"),
        "gate": proc.get("gate"),
        "stratum_size": comp.get("stratum_size"),
        "judge": (report.get("judge") or {}).get("model"),
        "status": report.get("status"),
    }


def strip_assay_scores(scores: dict[str, Any]) -> dict[str, Any]:
    """Remove this module's fields, leaving the harness's alone."""
    for f in ASSAY_SCORE_FIELDS:
        scores.pop(f, None)
    return scores
