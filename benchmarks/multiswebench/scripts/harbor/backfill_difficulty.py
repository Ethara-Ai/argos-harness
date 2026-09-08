"""Backfill difficulty into a delivered bundle's task.toml from its rollout.

Difficulty is the reference model's pass rate under the sealed-environment
protocol, not a property of the patch, so it cannot be known when the bundle is
built: converter.py emits ``unbanded`` and this pass replaces it once the
reference run has been scored.

Usage:
    uv run python -m benchmarks.multiswebench.scripts.harbor.backfill_difficulty \
        argos_bundles/<uuid> [more_bundle_dirs ...]

One reference model defines the tier -- ``opus-5``, overridable with --model.
The band is the mean score_eval of that model's runs and is written as the
single ``difficulty`` key; runs by any other model in the same bundle are
reported and ignored, and legacy per-model ``difficulty_<model>`` keys are
dropped. Cut points are unchanged, so a mean-based band reads lower than the
pass-rate band the thresholds were drawn for.

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
from statistics import mean
from typing import NamedTuple

from benchmarks.multiswebench.scripts.harbor.converter import (
    DIFFICULTY_TIERS,
    map_difficulty,
)


RESULT_GLOB = "trajectories/*/run_*/result.json"

# The one model whose pass rate defines the tier. A bundle may hold runs by
# other models for comparison, but they do not move the shipped difficulty.
REFERENCE_MODEL = "opus-5"

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


# Anchored to line start to avoid matching prose, and spaced with [ \t] rather
# than \s so neither pattern can run past its own line. The trailing newline is
# optional only so a key on the final line still matches.
_DIFFICULTY_RE = re.compile(r'^difficulty[ \t]*=[ \t]*"[^"]*"\n?', re.MULTILINE)
_MODEL_DIFFICULTY_RE = re.compile(
    r'^difficulty_[0-9a-z]+[ \t]*=[ \t]*"[^"]*"\n?', re.MULTILINE
)
_METADATA_RE = re.compile(r"^\[metadata\]", re.MULTILINE)


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


def model_of(result_path: Path) -> str:
    """``trajectories/<model>/run_N/result.json`` -> ``<model>``."""
    return result_path.parent.parent.name


def scored_runs(bundle: Path) -> list[tuple[Path, str, float]]:
    return [
        (path, model_of(path), rate)
        for path in run_results(bundle)
        if (rate := read_pass_rate(path)) is not None
    ]


def write_difficulty(task_toml: Path, band: str) -> bool:
    """Upsert the scalar ``difficulty`` key. False when unchanged.

    One reference model defines the tier, so the per-model
    ``difficulty_<model>`` keys have nothing left to say and are dropped
    wherever an older bundle still carries them.
    """
    content = task_toml.read_text(encoding="utf-8")
    if not _METADATA_RE.search(content):
        raise ValueError(f"no [metadata] table in {task_toml}")

    line = f'difficulty = "{band}"'
    updated = _MODEL_DIFFICULTY_RE.sub("", content)
    if _DIFFICULTY_RE.search(updated):
        updated = _DIFFICULTY_RE.sub(line + "\n", updated, count=1)
    else:
        updated = _METADATA_RE.sub(lambda m: m.group(0) + "\n" + line, updated, count=1)

    if updated == content:
        return False
    task_toml.write_text(updated, encoding="utf-8")
    return True


def backfill_bundle(
    bundle: Path,
    *,
    preflight: bool = False,
    expect_runs: int | None = None,
    model: str = REFERENCE_MODEL,
) -> Outcome:
    """``ok`` is False for anything a delivery should look at."""
    task_toml = bundle / "task.toml"
    if not task_toml.is_file():
        return Outcome(False, "no task.toml", None)

    results = run_results(bundle)
    found = sum(1 for path in results if model_of(path) == model)
    if not found:
        others = sorted({model_of(path) for path in results})
        seen = f", found {', '.join(others)}" if others else ""
        return Outcome(False, f"no {model} run{seen}", None)

    rates = [rate for _, run_model, rate in scored_runs(bundle) if run_model == model]
    if not rates:
        return Outcome(
            False, f"no scored {model} run ({found} result.json found)", None
        )

    if expect_runs is not None and len(rates) != expect_runs:
        return Outcome(
            False,
            f"expected {expect_runs} scored {model} runs, got {len(rates)}",
            None,
        )

    band = map_difficulty(mean(rates))
    complete = len(rates) == found
    detail = f"{model}: mean={mean(rates):.4f} n={len(rates)} -> {band}"
    ignored = sorted({model_of(path) for path in results} - {model})
    if ignored:
        detail += f" (ignored {', '.join(ignored)})"
    if not complete:
        detail += f" [PARTIAL {len(rates)}/{found} runs scored]"

    if preflight:
        return Outcome(complete, f"would set {detail}", band)

    changed = write_difficulty(task_toml, band)
    verb = "set" if changed else "already"
    return Outcome(complete, f"{verb} {detail}", band)


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("bundle_dirs", nargs="+", type=Path)
    parser.add_argument(
        "--model",
        default=REFERENCE_MODEL,
        help=f"reference model whose runs define the tier (default {REFERENCE_MODEL})",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="report what would change and write nothing",
    )
    parser.add_argument(
        "--expect-runs",
        type=int,
        metavar="N",
        help="refuse any bundle without exactly N scored reference runs "
        "(1 = pass@1, 8 = pass@8)",
    )
    parser.add_argument(
        "--enforce-mix",
        action="store_true",
        # argparse %-expands help text, so the literal sign must be doubled.
        help=f"fail when any tier is more than {MIX_TOLERANCE * 100:.0f}%% "
        "off the target mix",
    )
    args = parser.parse_args()

    flagged = 0
    counts: Counter[str] = Counter()
    for bundle in args.bundle_dirs:
        result = backfill_bundle(
            bundle,
            preflight=args.preflight,
            expect_runs=args.expect_runs,
            model=args.model,
        )
        if not result.ok:
            flagged += 1
        if result.difficulty is not None:
            counts.update([result.difficulty])
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
