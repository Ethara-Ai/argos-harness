"""Rubric validation: is this rubric actually any good.

A rubric nobody validated is decoration. Two measurements, both computable from
recorded runs with no new rollouts. The third, the adversarial null, lives in
``tests/test_adversarial.py``.

1. **Inter-judge agreement**, per item. Below the floor, an item is re-authored
   rather than argued about.
2. **Discrimination**, the correlation between an item's verdict and the outcome.
   The reading is not "higher is better". An item that correlates near zero *and*
   has high agreement is the valuable kind, because it measures something the
   outcome score misses, which is the entire reason this channel exists. The item
   to cut is the one with low agreement *and* no correlation: that is noise.

Small-sample honesty: with a handful of graded runs these numbers are indicative
and nothing more. ``n`` is printed beside every statistic so a reader can see that
rather than take a decimal at face value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .rubric import ItemOutcome, RubricReport


AGREEMENT_FLOOR = 0.60


@dataclass(frozen=True)
class ItemStats:
    item_id: str
    n: int
    agreement: float | None
    yes_rate: float
    point_biserial: float | None
    abstention_rate: float
    dissent_filtered: int = 0

    @property
    def reading(self) -> str:
        if self.n < 2:
            return "insufficient data"
        if self.agreement is not None and self.agreement < AGREEMENT_FLOOR:
            return "re-author, judges do not agree"
        if self.yes_rate in (0.0, 1.0):
            return "no variance on this sample"
        if self.point_biserial is not None and abs(self.point_biserial) < 0.2:
            return "independent of outcome, the useful kind"
        return "tracks outcome"


def item_stats(
    reports: Sequence[RubricReport], outcomes: Sequence[float]
) -> list[ItemStats]:
    by_item: dict[str, list[tuple[ItemOutcome, float]]] = {}
    for rep, out in zip(reports, outcomes):
        for o in rep.outcomes:
            by_item.setdefault(o.item.id, []).append((o, out))

    stats: list[ItemStats] = []
    for iid, rows in by_item.items():
        scored = [
            (o, out) for o, out in rows if not o.abstained and o.satisfied is not None
        ]
        n = len(scored)
        abst = 1.0 - (n / len(rows)) if rows else 0.0
        ags = [_agreement(o) for o, _ in rows if len(o.verdicts) >= 2]
        agreement = sum(ags) / len(ags) if ags else None
        yes = [1.0 if o.satisfied else 0.0 for o, _ in scored]
        stats.append(
            ItemStats(
                item_id=iid,
                n=n,
                agreement=agreement,
                yes_rate=(sum(yes) / n if n else 0.0),
                point_biserial=_point_biserial(yes, [out for _, out in scored]),
                abstention_rate=abst,
                dissent_filtered=sum(1 for o, _ in rows if o.dissent_filtered),
            )
        )
    return sorted(stats, key=lambda s: s.item_id)


def _agreement(o: ItemOutcome) -> float:
    """Agreement is only defined among two or more judges.

    Under per-subject routing most runs meet a single judge, and a lone judge
    agrees with itself by construction. Averaging that 1.0 in would report the
    routing decision as a consensus finding, and it would climb as the panel
    shrank - the diagnostic looks best exactly where there is least evidence.
    Callers filter to multi-judge outcomes, so this never sees a single verdict.
    """
    vals = [1 if v.satisfied else 0 for v in o.verdicts]
    if not vals:
        return 0.0
    return max(vals.count(1), vals.count(0)) / len(vals)


def _point_biserial(
    binary: Sequence[float], continuous: Sequence[float]
) -> float | None:
    n = len(binary)
    if n < 3 or len(set(binary)) < 2:
        return None
    mean_c = sum(continuous) / n
    sd = math.sqrt(sum((c - mean_c) ** 2 for c in continuous) / n)
    if sd == 0:
        return None
    ones = [c for b, c in zip(binary, continuous) if b == 1.0]
    zeros = [c for b, c in zip(binary, continuous) if b == 0.0]
    m1, m0 = sum(ones) / len(ones), sum(zeros) / len(zeros)
    p, q = len(ones) / n, len(zeros) / n
    return (m1 - m0) / sd * math.sqrt(p * q)


def render(stats: Sequence[ItemStats], n_runs: int, n_members: int) -> str:
    lines = [
        f"rubric validation over {n_runs} graded run(s), {n_members} council member(s)",
        "",
        f"{'item':6} {'n':>3} {'agree':>6} {'yes%':>6} {'r_pb':>7} {'abst%':>6} {'filt':>4}  reading",
        "-" * 90,
    ]
    for s in stats:
        ag = f"{s.agreement:.2f}" if s.agreement is not None else "  -  "
        pb = f"{s.point_biserial:+.2f}" if s.point_biserial is not None else "  -   "
        lines.append(
            f"{s.item_id:6} {s.n:>3} {ag:>6} {s.yes_rate * 100:>5.0f}% "
            f"{pb:>7} {s.abstention_rate * 100:>5.0f}% {s.dissent_filtered:>4}  "
            f"{s.reading}"
        )
    lines += [
        "",
        "agree = fraction of ALL members that agreed, averaged over runs",
        "yes%  = how often the criterion was answered Yes",
        "r_pb  = point-biserial correlation with the outcome score",
        "abst% = fraction of runs where the item abstained",
        "filt  = runs where a dropped verdict disagreed with the one that stood.",
        "        Nonzero here means the agreement figure is real disagreement that",
        "        the truncation or citation filter removed, not consensus.",
        "",
        "An item independent of outcome is not a defect. It is the point.",
    ]
    return "\n".join(lines)
