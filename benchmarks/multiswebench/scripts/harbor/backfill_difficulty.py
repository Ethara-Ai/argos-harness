"""Backfill difficulty into a delivered bundle's task.toml from its rollout.

Difficulty is the reference model's pass rate under the sealed-environment
protocol, not a property of the patch, so it cannot be known when the bundle is
built: converter.py emits ``unbanded`` and this pass replaces it once the
reference run has been scored.

Usage:
    uv run python -m benchmarks.multiswebench.scripts.harbor.backfill_difficulty \
        argos_bundles/<uuid> [more_bundle_dirs ...]

One scored run is pass@1 and bands on that run. Several runs are pass@k, which
resolves to the best attempt, so the band is taken from the highest score_eval
-- never the mean, which would understate a task the models can in fact solve.
Both cases are the same max(); pass@1 is just k=1.

Run --preflight first. A bundle whose runs did not all score still bands, from
the runs that did, and a missing run can only ever have raised the maximum --
so a partial pass@8 silently labels the task harder than it is. --expect-runs
turns that into a refusal instead of a wrong tier.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from benchmarks.multiswebench.scripts.harbor.converter import (
    DIFFICULTY_TIERS,
    map_difficulty,
)


RESULT_GLOB = "trajectories/*/run_*/result.json"

# Client-specified batch composition. Trivial is banded but not shipped, so its
# share is zero rather than absent: a trivial task in the batch is a finding.
TARGET_MIX: dict[str, float] = {
    "trivial": 0.00,
    "easy": 0.10,
    "medium": 0.30,
    "hard": 0.40,
    "expert": 0.20,
}
MIX_TOLERANCE = 0.05


class Outcome(NamedTuple):
    ok: bool
    message: str
    difficulty: str | None


# Only the value is rewritten, so the key's position and the rest of the file
# survive byte-for-byte. Anchored to line start to avoid matching prose.
_DIFFICULTY_RE = re.compile(r'^(difficulty\s*=\s*)"[^"]*"', re.MULTILINE)


def read_pass_rate(result_path: Path) -> float | None:
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scores = (data.get("verifier_result") or {}).get("scores") or {}
    value = scores.get("score_eval")
    return float(value) if isinstance(value, (int, float)) else None


def run_results(bundle: Path) -> list[Path]:
    return sorted(bundle.glob(RESULT_GLOB))


def scored_runs(bundle: Path) -> list[tuple[Path, float]]:
    return [
        (path, rate)
        for path in run_results(bundle)
        if (rate := read_pass_rate(path)) is not None
    ]


def write_difficulty(task_toml: Path, difficulty: str) -> bool:
    """Replace the difficulty value in place. False when already correct."""
    content = task_toml.read_text(encoding="utf-8")
    updated, count = _DIFFICULTY_RE.subn(rf'\g<1>"{difficulty}"', content)
    if not count:
        raise ValueError(f"no difficulty key in {task_toml}")
    if updated == content:
        return False
    task_toml.write_text(updated, encoding="utf-8")
    return True


def backfill_bundle(
    bundle: Path, *, preflight: bool = False, expect_runs: int | None = None
) -> Outcome:
    """``ok`` is False for anything a delivery should look at."""
    task_toml = bundle / "task.toml"
    if not task_toml.is_file():
        return Outcome(False, "no task.toml", None)

    found = len(run_results(bundle))
    runs = scored_runs(bundle)
    if not runs:
        return Outcome(False, f"no scored run ({found} result.json found)", None)

    partial = f"{len(runs)}/{found} runs scored"
    if expect_runs is not None and len(runs) != expect_runs:
        return Outcome(
            False, f"expected {expect_runs} scored runs, got {partial}", None
        )

    pass_rate = max(rate for _, rate in runs)
    difficulty = map_difficulty(pass_rate)
    complete = len(runs) == found
    detail = f"pass@{len(runs)} best={pass_rate:.4f}"
    if not complete:
        detail += f" [PARTIAL {partial}]"

    if preflight:
        current = _current_difficulty(task_toml)
        change = "unchanged" if current == difficulty else f"{current} -> {difficulty}"
        return Outcome(
            complete, f"would set {difficulty} ({change}; {detail})", difficulty
        )

    changed = write_difficulty(task_toml, difficulty)
    verb = "set" if changed else "already"
    return Outcome(complete, f"{verb} {difficulty} ({detail})", difficulty)


def mix_report(counts: Counter[str]) -> tuple[bool, list[str]]:
    """Batch composition against TARGET_MIX. False when any tier is off band."""
    total = sum(counts.values())
    lines = [f"{'tier':8s} {'n':>4s} {'share':>7s} {'target':>7s} {'delta':>7s}"]
    within = True
    for tier in DIFFICULTY_TIERS:
        n = counts.get(tier, 0)
        share = n / total if total else 0.0
        target = TARGET_MIX[tier]
        delta = share - target
        off = abs(delta) > MIX_TOLERANCE
        within = within and not off
        flag = "  <-- off" if off else ""
        lines.append(f"{tier:8s} {n:4d} {share:6.0%} {target:6.0%} {delta:+6.0%}{flag}")
    return within, lines


def _current_difficulty(task_toml: Path) -> str:
    match = _DIFFICULTY_RE.search(task_toml.read_text(encoding="utf-8"))
    return match.group(0).split('"')[1] if match else "<missing>"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("bundle_dirs", nargs="+", type=Path)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="report what would change and write nothing",
    )
    parser.add_argument(
        "--expect-runs",
        type=int,
        metavar="N",
        help="refuse any bundle without exactly N scored runs (1 = pass@1, 8 = pass@8)",
    )
    parser.add_argument(
        "--enforce-mix",
        action="store_true",
        help=f"fail when any tier is more than {MIX_TOLERANCE:.0%} off the target mix",
    )
    args = parser.parse_args()

    flagged = 0
    counts: Counter[str] = Counter()
    for bundle in args.bundle_dirs:
        result = backfill_bundle(
            bundle, preflight=args.preflight, expect_runs=args.expect_runs
        )
        if not result.ok:
            flagged += 1
        if result.difficulty is not None:
            counts[result.difficulty] += 1
        print(f"{'  ' if result.ok else '! '}{bundle.name}: {result.message}")

    within, lines = mix_report(counts)
    print("\nbatch distribution")
    for line in lines:
        print(f"  {line}")

    if flagged:
        print(f"\n{flagged}/{len(args.bundle_dirs)} bundle(s) need review")
    if args.enforce_mix and not within:
        print("batch distribution is outside the target mix")
    return 1 if flagged or (args.enforce_mix and not within) else 0


if __name__ == "__main__":
    raise SystemExit(main())
