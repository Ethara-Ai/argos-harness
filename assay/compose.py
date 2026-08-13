"""Compose outcome and process into one score.

Three constants that were hand-set are now derived. The channel split follows
measured inter-rater agreement rather than the fixed 0.7/0.3, whose stated
justification ("no measured kappa") went stale once kappa was measured on 297
runs and which only ever argued for a direction, not a value. The shaping width
follows the outcome gaps a task actually exhibits rather than a global 0.05,
whose per-target-test invariant held only for T <= 9 and failed on 9 of 30
tasks. The process term is normalized inside an outcome stratum, which is what
HERO (arXiv:2510.07242) actually contributes: because the bands do not overlap,
process structurally cannot move a run across the outcome boundary, where a
magnitude bound only caps how far it moves.
"""

from __future__ import annotations

from typing import Iterable, Sequence


__all__ = [
    "ALPHA_CAP",
    "FLAT_STRATUM",
    "channel_weight",
    "process_score",
    "alpha_for_group",
    "stratify",
    "combine",
    "rl_score",
    "eval_score",
]

# HERO 4.3: "For verifiable tasks, smaller reward ranges (e.g., alpha = 0.05)
# yielded the best results". The same section prefers 0.1-0.2 for mixed tasks
# where many samples fail the verifier, which this corpus arguably is, so the
# cap is the conservative reading rather than the obviously correct one.
ALPHA_CAP = 0.05

# Min-max stretches any stratum to the full range, so a stratum whose process
# scores differ trivially would be amplified into a full-range signal.
FLAT_STRATUM = 0.05


def channel_weight(kappa: float | None) -> float:
    return max(0.0, float(kappa)) if kappa is not None else 0.0


def process_score(*, det: float, rubric: float | None, kappa: float | None) -> float:
    w = 0.0 if rubric is None else channel_weight(kappa)
    return (det + w * (rubric or 0.0)) / (1.0 + w)


# Outcome differences below this are not a quality ordering worth protecting:
# on an 824-target task one flaky test is 0.0012, and honouring it as a real gap
# drove alpha to 0.0006 and left process weighing 0.0012. That silenced the
# channel on exactly the tasks where outcome saturates and process is the only
# thing that still separates runs.
MIN_MEANINGFUL_GAP = 0.02


def alpha_for_task(n_targets: int) -> float:
    """Shaping room for a task, independent of which runs exist.

    Deriving alpha from the observed run group made score_eval move when a peer
    was added: three gpt-5.6 runs shifted alpha from 0.05 to 0.0098 and an
    untouched opus run's score changed by 0.028, which is the one thing
    score_eval promises never happens. The outcome channel's own resolution is
    one target test, so that is the gap alpha respects, floored where a single
    test of many is too small to be a real ordering. score_rl stays
    group-relative; that is what it is for.
    """
    unit = 1.0 / n_targets if n_targets else 1.0
    d = max(unit, MIN_MEANINGFUL_GAP)
    return min(ALPHA_CAP, d / (2.0 * (1.0 + d)))


def alpha_for_gaps(outcomes: Sequence[float]) -> float:
    """Widest shaping that still cannot bridge a meaningful outcome gap."""
    vals = sorted({round(float(o), 9) for o in outcomes if o is not None})
    gaps = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    if not gaps:
        return 0.5
    d = max(min(gaps), MIN_MEANINGFUL_GAP)
    return min(ALPHA_CAP, d / (2.0 * (1.0 + d)))


def alpha_for_group(outcomes: Sequence[float]) -> float:
    return alpha_for_gaps(outcomes)


def stratify(process: float, stratum_processes: Iterable[float]) -> float:
    vals = [float(p) for p in stratum_processes]
    if not vals:
        return 0.5
    lo, hi = min(vals), max(vals)
    if hi - lo < FLAT_STRATUM:
        return 0.5
    return (float(process) - lo) / (hi - lo)


def combine(*, outcome: float, p: float, alpha: float, gate: int) -> float:
    return gate * ((1.0 - 2.0 * alpha) * outcome + 2.0 * alpha * p)


def eval_score(
    *,
    outcome: float,
    det: float,
    rubric: float | None,
    kappa: float | None,
    alpha: float,
    gate: int,
) -> float:
    """Absolute and comparable: a run's score never moves because a peer changed."""
    p = process_score(det=det, rubric=rubric, kappa=kappa)
    return combine(outcome=outcome, p=p, alpha=alpha, gate=gate)


def rl_score(
    *,
    outcome: float,
    det: float,
    rubric: float | None,
    kappa: float | None,
    alpha: float,
    gate: int,
    stratum_processes: Sequence[float],
) -> float:
    """Group-relative: only within-stratum ordering carries, as GRPO expects."""
    p = process_score(det=det, rubric=rubric, kappa=kappa)
    return combine(
        outcome=outcome, p=stratify(p, stratum_processes), alpha=alpha, gate=gate
    )
