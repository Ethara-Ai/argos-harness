"""Compose outcome and process into one score.

The reward runs on a single LLM judge, so the rubric channel's weight is a
fixed, documented constant rather than the measured inter-rater agreement it
used to be: kappa exists only to discount a council when its members disagree,
and with one judge there is no agreement to measure — any value would be
invented (change spec "remove kappa, keep alpha"). The shaping width (alpha)
is unchanged: it follows the task's own outcome resolution so the process term
can rank equal-outcome runs without ever bridging a real outcome gap. The
process term is normalized inside an outcome stratum for score_rl, which is
what HERO (arXiv:2510.07242) actually contributes: because the bands do not
overlap, process structurally cannot move a run across the outcome boundary,
where a magnitude bound only caps how far it moves.
"""

from __future__ import annotations

from typing import Iterable, Sequence


__all__ = [
    "ALPHA_CAP",
    "FLAT_STRATUM",
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


def process_score(*, det: float, rubric: float | None) -> float:
    """Plain average of the two process channels: (det + rubric) / 2.

    The single judge and the rule checks count equally — no weight dial, no
    agreement statistic. An unjudged run has no rubric channel to average, so
    its process score is the deterministic channel alone.
    """
    if rubric is None:
        return det
    return (det + rubric) / 2.0


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
    alpha: float,
    gate: int,
) -> float:
    """Absolute and comparable: a run's score never moves because a peer changed."""
    p = process_score(det=det, rubric=rubric)
    return combine(outcome=outcome, p=p, alpha=alpha, gate=gate)


def rl_score(
    *,
    outcome: float,
    det: float,
    rubric: float | None,
    alpha: float,
    gate: int,
    stratum_processes: Sequence[float],
) -> float:
    """Group-relative: only within-stratum ordering carries, as GRPO expects."""
    p = process_score(det=det, rubric=rubric)
    return combine(
        outcome=outcome, p=stratify(p, stratum_processes), alpha=alpha, gate=gate
    )
