"""Rubric score math — single source of truth, mirroring score_v2g.py's contract.

Side-effect-free: importing this module performs no I/O, and
``compute_rubric_score`` is a pure function of its arguments.

Semantics (locked):

* ``passed = (not satisfied) if negative else satisfied`` — polarity lives in
  the sign of the item's score, never in the judge's head.
* ``numerator``  = Σ score of *satisfied* non-abstained items. Keying on the
  judge's ``satisfied`` (not on post-polarity ``passed``) is deliberate: a
  satisfied negative item contributes its negative score and subtracts. Keying
  on ``passed`` looks right and silently disarms every guardrail (both signs
  come out backwards).
* ``denominator`` = Σ positive scores of non-abstained items only. Summing all
  weights (signed or absolute) lets a guardrail inflate the very denominator it
  is meant to penalise against.
* ``raw`` is unclamped (can be negative); ``score = clip(raw, 0, 1)``.
* A zero denominator yields ``None`` scores and ``needs_review`` — never a
  silent 0.0.
* ``abstain_ratio > abstain_review_threshold`` (strict) ⇒ ``needs_review``.
"""

from __future__ import annotations

import math
from typing import Any


RUBRIC_SCORE_VERSION = "rubric_v1"
ABSTAIN_REVIEW_THRESHOLD = 0.30

#: abstain reasons the runner/parser may set; anything else is normalized to
#: "other" so the report vocabulary stays closed.
KNOWN_ABSTAIN_REASONS = frozenset(
    {
        "parse-miss",
        "ambiguous-verdict",
        "truncation",
        "judge-error",
        "judge-truncated",
        "evidence-missing",
        "other",
    }
)


def _is_finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _item_score(raw: dict[str, Any]) -> int:
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return 0
    return score


def compute_rubric_score(
    items: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    *,
    abstain_review_threshold: float = ABSTAIN_REVIEW_THRESHOLD,
) -> dict[str, Any]:
    """Score one run's rubric verdicts.

    ``items`` — the rubric's ``rubrics`` list (structurally validated).
    ``verdicts`` — keyed by item ``number``; each value is
    ``{"satisfied": bool|None, "rationale": str, "truncation_affected": bool,
    "abstain_reason": str|None}``. A missing key or ``satisfied is None`` is an
    abstention (defense in depth — the parser already guarantees presence).

    Returns ``{"scores": {"score", "raw"}, "score_version", "status",
    "status_reasons", "per_criterion", "diagnostics"}`` with status one of
    ``scored | needs_review | invalid_rubric | no_signal``.
    """
    status_reasons: list[str] = []
    per_criterion: list[dict[str, Any]] = []

    if not items:
        return {
            "scores": {"score": None, "raw": None},
            "score_version": RUBRIC_SCORE_VERSION,
            "status": "no_signal",
            "status_reasons": ["empty_rubric"],
            "per_criterion": [],
            "diagnostics": {
                "items_total": 0,
                "abstained_total": 0,
                "abstain_ratio": 0.0,
                "denominator": 0,
                "numerator": 0,
                "negative_hits": 0,
                "positive_weight_total": 0,
            },
        }

    positive_weight_total = sum(s for s in (_item_score(i) for i in items) if s > 0)

    numerator = 0
    denominator = 0
    abstained_total = 0
    negative_hits = 0

    for item in items:
        number = str(item.get("number") or "")
        score = _item_score(item)
        is_positive = score > 0
        verdict = verdicts.get(number)

        satisfied: bool | None = None
        rationale = ""
        truncation_affected = False
        abstain_reason: str | None = None

        if verdict is None:
            abstain_reason = "parse-miss"
        else:
            raw_satisfied = verdict.get("satisfied")
            rationale = str(verdict.get("rationale") or "")
            truncation_affected = bool(verdict.get("truncation_affected"))
            raw_reason = verdict.get("abstain_reason")
            if raw_reason:
                abstain_reason = (
                    str(raw_reason)
                    if str(raw_reason) in KNOWN_ABSTAIN_REASONS
                    else "other"
                )
            elif isinstance(raw_satisfied, bool):
                satisfied = raw_satisfied
            else:
                abstain_reason = "parse-miss"

        abstained = abstain_reason is not None
        passed: bool | None = None
        if not abstained and satisfied is not None:
            passed = (not satisfied) if not is_positive else satisfied
            if satisfied:
                numerator += score
                if not is_positive:
                    negative_hits += 1
            if is_positive:
                denominator += score
        else:
            abstained_total += 1

        per_criterion.append(
            {
                "number": number,
                "score": score,
                "is_positive": is_positive,
                "satisfied": satisfied,
                "passed": passed,
                "abstained": abstained,
                "abstain_reason": abstain_reason,
                "truncation_affected": truncation_affected,
                "rationale": rationale[:2000],
            }
        )

    items_total = len(items)
    abstain_ratio = abstained_total / items_total

    diagnostics = {
        "items_total": items_total,
        "abstained_total": abstained_total,
        "abstain_ratio": round(abstain_ratio, 4),
        "denominator": denominator,
        "numerator": numerator,
        "negative_hits": negative_hits,
        "positive_weight_total": positive_weight_total,
    }

    if positive_weight_total <= 0:
        # Lint L4 blocks this upstream; runtime guard so a hand-edited rubric
        # can never divide by a signed mass.
        return {
            "scores": {"score": None, "raw": None},
            "score_version": RUBRIC_SCORE_VERSION,
            "status": "invalid_rubric",
            "status_reasons": ["no_positive_items"],
            "per_criterion": per_criterion,
            "diagnostics": diagnostics,
        }

    if denominator <= 0:
        status_reasons.append("zero_denominator")
        if abstained_total == items_total:
            status_reasons.append("all_abstained")
        return {
            "scores": {"score": None, "raw": None},
            "score_version": RUBRIC_SCORE_VERSION,
            "status": "needs_review",
            "status_reasons": status_reasons,
            "per_criterion": per_criterion,
            "diagnostics": diagnostics,
        }

    raw = numerator / denominator
    if not _is_finite(raw):
        return {
            "scores": {"score": None, "raw": None},
            "score_version": RUBRIC_SCORE_VERSION,
            "status": "needs_review",
            "status_reasons": ["non_finite"],
            "per_criterion": per_criterion,
            "diagnostics": diagnostics,
        }

    score = min(1.0, max(0.0, raw))
    status = "scored"
    if abstain_ratio > abstain_review_threshold:
        status = "needs_review"
        status_reasons.append("abstain_ratio_exceeded")

    return {
        "scores": {"score": round(score, 4), "raw": round(raw, 6)},
        "score_version": RUBRIC_SCORE_VERSION,
        "status": status,
        "status_reasons": status_reasons,
        "per_criterion": per_criterion,
        "diagnostics": diagnostics,
    }
