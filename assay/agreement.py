"""Inter-rater reliability for the judge council.

Raw agreement is not reliability. Rubric items are heavily skewed - most runs
fail most items - so two judges that both answer No every time agree 100% of
the time while carrying no information. Cohen's kappa corrects for that chance
agreement, and is reported as ``None`` rather than 1.0 when the correction is
undefined, so a degenerate item set cannot read as a perfect council.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping, Sequence


__all__ = ["cohen_kappa", "council_agreement", "pooled_kappa"]


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    if len(a) != len(b):
        raise ValueError(f"rater lengths differ: {len(a)} != {len(b)}")
    n = len(a)
    if n < 2:
        return None

    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = sum(
        (sum(1 for x in a if x is c) / n) * (sum(1 for y in b if y is c) / n)
        for c in (True, False)
    )
    if abs(1.0 - expected) < 1e-12:
        return None
    return round((observed - expected) / (1.0 - expected), 4)


def council_agreement(observations: Mapping[str, Mapping[str, bool]]) -> dict[str, Any]:
    members = sorted({m for votes in observations.values() for m in votes})
    multi = {i: v for i, v in observations.items() if len(v) >= 2}

    if len(members) < 2 or not multi:
        note = (
            f"{len(members)} judge(s). Agreement and kappa need at least two "
            "judges on the same item; a one-member score is a single-judge "
            "score, not a council."
        )
        return {
            "n_judges": len(members),
            "members": members,
            "n_items": len(multi),
            "raw_agreement": None,
            "kappa": None,
            "pairs": {},
            "note": note,
        }

    raw = sum(1 for v in multi.values() if len(set(v.values())) == 1) / len(multi)

    pairs: dict[str, float | None] = {}
    for x, y in combinations(members, 2):
        both = [i for i, v in multi.items() if x in v and y in v]
        if len(both) < 2:
            continue
        pairs[f"{x}|{y}"] = cohen_kappa(
            [multi[i][x] for i in both], [multi[i][y] for i in both]
        )

    defined = [k for k in pairs.values() if k is not None]
    kappa = round(sum(defined) / len(defined), 4) if defined else None

    if kappa is None:
        note = (
            f"{len(members)} judges over {len(multi)} co-rated items. Kappa is "
            "degenerate: the judges used a single label throughout, so chance "
            f"agreement is 1.0 and raw agreement of {raw:.2f} carries no "
            "reliability information."
        )
    else:
        note = (
            f"{len(members)} judges over {len(multi)} co-rated items; mean "
            f"pairwise Cohen kappa {kappa:.2f} across {len(defined)} defined pair(s)."
        )

    return {
        "n_judges": len(members),
        "members": members,
        "n_items": len(multi),
        "raw_agreement": round(raw, 4),
        "kappa": kappa,
        "pairs": pairs,
        "note": note,
    }


def pooled_kappa(runs: Sequence[Mapping[str, Mapping[str, bool]]]) -> float | None:
    """Judge agreement for a whole task, pooled over every co-rated decision.

    Kappa is a property of the criteria, not of a run: it asks whether two
    independent readers of the same rubric reach the same answer. Estimating it
    per run spends a 13-item sample on a statistic that needs more, and lets two
    runs of one task weigh the judge channel differently for no reason but noise.

    Pooling matters more since judges are chosen per subject. Most runs meet a
    single judge and can contribute nothing, so a task's estimate rests entirely
    on the runs both judges scored - a few dozen decisions instead of a dozen.
    """
    pairs: dict[tuple[str, str], tuple[list[bool], list[bool]]] = {}
    for observations in runs:
        for votes in observations.values():
            for x, y in combinations(sorted(votes), 2):
                a, b = pairs.setdefault((x, y), ([], []))
                a.append(votes[x])
                b.append(votes[y])
    defined = [
        k for k in (cohen_kappa(a, b) for a, b in pairs.values()) if k is not None
    ]
    return round(sum(defined) / len(defined), 4) if defined else None
