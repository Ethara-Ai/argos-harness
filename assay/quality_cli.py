"""``assay quality-*``: the code-quality channel's commands.

Kept beside, not inside, ``cli.py``: the rubric commands derive their roster
from ``ASSAY_COUNCIL`` and the corpus defaults in ``assay.judge``; these take
the judge explicitly (``--judge-config``) and refuse anything else, so a run
outside ``run_eval.sh`` can never grade quality with a roster it did not name.

Every command reads the bundle's own ``tests/quality.json`` and fails closed
when it is absent; the package manifest is only ever the source that
``quality-init`` copies from.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bundle import RunBundle, TaskBundle
from .judge import (
    ITEM_DEADLINE,
    BridgeUnreachable,
    SubscriptionCapped,
    call_with_backoff,
    set_pace_for_workers,
)
from .quality import (
    DEFAULT_GATES,
    FINGERPRINT_KEY,
    GOLD_LABEL,
    MANIFEST_PATH,
    QUALITY_SYSTEM,
    QUALITY_VERSION,
    JudgeSeat,
    ManifestMissing,
    QualityReport,
    balanced_accuracy,
    build_quality_packet,
    build_report,
    calibration_check,
    calibration_passes,
    evidence_digest,
    load_quality_items,
    manifest_digest,
    pearson,
    prompt_digest,
    quality_fingerprint,
    read_records,
    replay_outcomes,
    resolve_single_judge,
    spearman,
    upsert_quality_block,
    verdict_file,
    weighted_kappa,
)
from .rubric import build_prompt_parts, parse_verdict
from .writeback import merge_quality


QUALITY_STORE = "quality"


# -- shared ---------------------------------------------------------------------


def _task(args: argparse.Namespace) -> TaskBundle:
    from .cli import _task as cli_task

    return cli_task(args)


def _runs(args: argparse.Namespace, task: TaskBundle) -> list[RunBundle]:
    from .cli import _runs as cli_runs

    return cli_runs(args, task)


def _bundle_root(delivery: Path) -> Path:
    nested = Path(delivery) / "dataset"
    return nested if nested.is_dir() else Path(delivery)


def _bundles(delivery: Path) -> list[TaskBundle]:
    base = _bundle_root(delivery)
    return [
        TaskBundle(p)
        for p in sorted(base.iterdir())
        if p.is_dir() and (p / "tests").is_dir()
    ]


def _seat(args: argparse.Namespace) -> JudgeSeat:
    return resolve_single_judge(args.judge_config, args.proxy or None)


def _write_if_changed(dest: Path, data: bytes) -> bool:
    if dest.is_file() and dest.read_bytes() == data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def quality_store(given: str | Path, uuid: str) -> Path:
    """``<store>/quality/<uuid>``, whatever shape of the store was given.

    Quality verdicts must not share the rubric's per-task directory: the
    rubric scorer reads every ``*.jsonl`` there to agree on one
    ``bundle_fingerprint``, and a quality file (which carries a different
    key) would make it refuse the whole task.
    """
    given = Path(given)
    if given.name == uuid or given.name == QUALITY_STORE:
        given = given.parent
    return given / QUALITY_STORE / uuid


def _store(args: argparse.Namespace, task: TaskBundle) -> Path:
    given = (
        Path(args.verdicts) if args.verdicts else task.root.parent.parent / "verdicts"
    )
    return quality_store(given, task.uuid)


def _require_manifest(task: TaskBundle) -> Path:
    if not task.quality_path.is_file():
        raise ManifestMissing(
            f"{task.uuid}: no tests/quality.json; run `assay quality-init --task "
            f"{task.uuid}` first"
        )
    return task.quality_path


# -- quality-init -------------------------------------------------------------------


def cmd_quality_init(args: argparse.Namespace) -> int:
    tasks = [_task(args)] if args.task else _bundles(Path(args.delivery))
    if not tasks:
        print(f"quality-init: no bundles under {args.delivery}", file=sys.stderr)
        return 1
    data = MANIFEST_PATH.read_bytes()
    changed = 0
    for task in tasks:
        if _write_if_changed(task.quality_path, data):
            changed += 1
    print(f"quality-init: {len(tasks)} bundle(s), {changed} written")
    return 0


# -- quality-judge ------------------------------------------------------------------


def _subjects(
    args: argparse.Namespace, task: TaskBundle
) -> list[tuple[str, str | None, str | None]]:
    """(label, patch, sha256) per thing to judge; None patch means no evidence."""
    if args.gold:
        return [(GOLD_LABEL, task.fix_patch, task.fix_patch_sha256)]
    return [
        (run.label, run.agent_patch, run.agent_patch_sha256)
        for run in _runs(args, task)
    ]


def _usable(rec: dict[str, Any], member: str) -> bool:
    """A recorded answer that can decide its item: parseable, cited, not cut.

    The rubric channel keys resume on parseability alone because an uncited
    answer there merely leaves the denominator. Here every item must decide,
    so an uncited or truncation-affected answer is re-asked on the next pass
    rather than left to block the run forever.
    """
    v = parse_verdict(str(rec.get("completion") or ""), str(rec["item_id"]), member)
    return v is not None and bool(v.evidence_ref) and not v.truncation_affected


def _keep_current(path: Path, fp: str, member: str) -> set[str]:
    """Drop records from another era; return item ids already decided."""
    records = read_records(path)
    current = [r for r in records if r.get(FINGERPRINT_KEY) == fp]
    if len(current) != len(records):
        print(f"  {path.name}: dropped {len(records) - len(current)} stale record(s)")
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in current), encoding="utf-8"
        )
    return {
        str(r["item_id"])
        for r in current
        if r.get("member") == member and _usable(r, member)
    }


def cmd_quality_judge(args: argparse.Namespace) -> int:
    task = _task(args)
    seat = _seat(args)
    try:
        manifest = _require_manifest(task)
    except ManifestMissing as exc:
        print(f"REFUSE {exc}")
        return 2
    items = load_quality_items(manifest)
    prompt = prompt_digest(manifest)
    out = quality_store(
        Path(args.out) if args.out else task.root.parent.parent / "verdicts", task.uuid
    )
    out.mkdir(parents=True, exist_ok=True)
    subjects = _subjects(args, task)
    print(
        f"{task.instance_id}: quality {QUALITY_VERSION} prompt {prompt} judge "
        f"{seat.alias} ({seat.model_id}) via {seat.endpoint} -> {out}"
    )
    set_pace_for_workers(args.workers)
    failures: list[str] = []
    skipped = 0

    def work(subject: tuple[str, str | None, str | None]) -> None:
        nonlocal skipped
        label, patch, _sha = subject
        if patch is None:
            print(f"  {label}: no agent.patch shipped, evidence missing")
            skipped += 1
            return
        if not patch.strip():
            print(f"  {label}: empty patch, nothing to review")
            skipped += 1
            return
        path = verdict_file(out, label)
        packet, _ = build_quality_packet(task.instruction, patch)
        fp = quality_fingerprint(
            judge_model=seat.model_id, evidence=evidence_digest(packet), prompt=prompt
        )
        done = _keep_current(path, fp, seat.alias)
        pending = [i for i in items if i.id not in done]
        if not pending:
            print(f"  {label}: complete")
            return
        for item in pending:
            evidence, question = build_prompt_parts(item, packet)
            try:
                raw, err = call_with_backoff(
                    seat.endpoint,
                    seat.model,
                    QUALITY_SYSTEM,
                    question,
                    evidence,
                    time.time() + ITEM_DEADLINE,
                )
            except (SubscriptionCapped, PermissionError, BridgeUnreachable) as exc:
                failures.append(f"{label}: {exc}")
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "member": seat.alias,
                            "model": seat.model,
                            "item_id": item.id,
                            "completion": raw,
                            "error": err,
                            FINGERPRINT_KEY: fp,
                        }
                    )
                    + "\n"
                )
        print(f"  done {label}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(work, subjects))

    if failures:
        print(f"\nFATAL: {failures[0]}")
        return 1
    print(f"\nwrote quality verdicts to {out} ({skipped} subject(s) without evidence)")
    return 0


# -- quality-score ------------------------------------------------------------------


def _report_for(
    task: TaskBundle,
    seat: JudgeSeat,
    label: str,
    patch: str | None,
    sha: str | None,
    store: Path,
    calibrated: bool,
) -> QualityReport:
    model, run_id = label.split("/", 1) if "/" in label else (label, label)
    return build_report(
        task_uuid=task.uuid,
        model=model,
        run_id=run_id,
        manifest_path=task.quality_path,
        instruction=task.instruction,
        patch=patch,
        patch_sha256=sha,
        records=read_records(verdict_file(store, label)),
        seat=seat,
        calibrated=calibrated,
    )


def quality_md_from_doc(doc: dict[str, Any]) -> str:
    """Render the final_score.md block from a written quality.json.

    ``score --write`` regenerates final_score.md from the rubric report and
    has no QualityReport in hand; this keeps the block reproducible from the
    artifact alone.
    """
    from .quality import QUALITY_MD_END, QUALITY_MD_START

    ver = doc.get("version") or {}
    judge = (doc.get("judge") or {}).get("member") or "none"
    status = str(doc.get("status"))
    shadow = "" if doc.get("calibrated") else " (shadow, uncalibrated)"
    head = (
        f"{QUALITY_MD_START}\n"
        "## Quality (publish-only, never part of reward)\n\n"
        f"Judge: {judge}  \n"
        f"Version: {ver.get('quality')}, prompt {ver.get('prompt_digest') or 'n/a'}  \n"
        f"Status: {status}{shadow}  \n\n"
    )
    rows = ["| dimension | verdict |", "|---|---|"]
    for it in doc.get("items") or []:
        sat = it.get("satisfied")
        v = "pass" if sat else ("fail" if sat is False else "abstain")
        rows.append(f"| {it.get('dimension')} ({it.get('id')}) | {v} |")
    score = doc.get("score")
    shown = f"{score:.4f}" if isinstance(score, (int, float)) else "null"
    rows.append(f"| **score_quality** = passed weight / total weight | {shown} |")
    return head + "\n".join(rows) + f"\n{QUALITY_MD_END}\n"


def _write_run_artifacts(
    run: RunBundle, rep: QualityReport, store: Path, calibrated: bool
) -> int:
    vd = run.root / "verifier"
    vd.mkdir(parents=True, exist_ok=True)
    doc = rep.to_dict()
    (vd / "quality.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    src = verdict_file(store, run.label)
    if src.is_file():
        shutil.copyfile(src, vd / "quality_verdicts.jsonl")
    md_path = vd / "final_score.md"
    md = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    md_path.write_text(upsert_quality_block(md, rep.render_md()), encoding="utf-8")
    return publish_quality_to_result_json(run.root, doc, publish_score=calibrated)


def publish_quality_to_result_json(
    run_dir: Path, doc: dict[str, Any], *, publish_score: bool
) -> int:
    path = Path(run_dir) / "result.json"
    if not path.is_file():
        return 0
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return 0
    vr = result.setdefault("verifier_result", {})
    before = json.dumps(vr, sort_keys=True)
    merge_quality(vr, doc, publish_score=publish_score)
    if json.dumps(vr, sort_keys=True) == before:
        return 0
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 1


def cmd_quality_score(args: argparse.Namespace) -> int:
    task = _task(args)
    seat = _seat(args)
    try:
        manifest = _require_manifest(task)
    except ManifestMissing as exc:
        print(f"REFUSE {exc}")
        return 2
    prompt = prompt_digest(manifest)
    store = _store(args, task)
    calibrated, why = calibration_check(
        task.calibration_path,
        judge_model=seat.model_id,
        prompt=prompt,
        manifest=manifest_digest(manifest),
    )

    if args.gold:
        rep = _report_for(
            task,
            seat,
            GOLD_LABEL,
            task.fix_patch,
            task.fix_patch_sha256,
            store,
            calibrated,
        )
        print(json.dumps(rep.to_dict(), indent=2))
        return 0 if rep.status == "scored" else 1

    if not calibrated and not args.shadow:
        print(
            f"REFUSE {task.uuid}: quality judge is not calibrated ({why}). "
            "Pass --shadow to score without publishing score_quality."
        )
        return 1

    reports: list[tuple[RunBundle, QualityReport]] = []
    for run in _runs(args, task):
        reports.append(
            (
                run,
                _report_for(
                    task,
                    seat,
                    run.label,
                    run.agent_patch,
                    run.agent_patch_sha256,
                    store,
                    calibrated,
                ),
            )
        )

    mode = "" if calibrated else "  [shadow: " + why + "]"
    print(f"\n{task.instance_id}  ({task.uuid})  quality judge {seat.alias}{mode}")
    for run, rep in reports:
        score = f"{rep.score:.4f}" if rep.score is not None else "—"
        extra = f"  {rep.reasons[0]}" if rep.reasons else ""
        print(f"  {run.label:32} {rep.status:16} {score}{extra}")

    if args.write:
        wrote = sum(
            _write_run_artifacts(run, rep, store, calibrated) for run, rep in reports
        )
        state = "published" if calibrated else "withheld: shadow"
        print(
            f"\nwrote quality artifacts into {len(reports)} run directories "
            f"({wrote} result.json updated, score_quality {state})"
        )
    if args.out:
        Path(args.out).write_text(
            json.dumps([rep.to_dict() for _, rep in reports], indent=2),
            encoding="utf-8",
        )
    return 1 if any(rep.status == "unjudged" for _, rep in reports) else 0


# -- quality-calibrate --------------------------------------------------------------


LABEL_COLUMNS = ("task_uuid", "subject", "dimension", "rating", "rater")


def _emit_template(args: argparse.Namespace) -> int:
    rows = []
    for task in _bundles(Path(args.delivery)):
        if not task.quality_path.is_file():
            continue
        for item in load_quality_items(task.quality_path):
            for rater in ("r1", "r2"):
                rows.append([task.uuid, GOLD_LABEL, item.dimension, "", rater])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LABEL_COLUMNS + ("split",))
        w.writerows([r + [""] for r in rows])
    print(f"quality-calibrate: wrote template with {len(rows)} rows to {out}")
    return 0


def subject_split(uuid: str, subject: str, holdout_fraction: float) -> str:
    """Per-subject, independent of every other subject: adding or removing
    labels cannot move a subject between dev and holdout."""
    h = int(hashlib.sha256(f"{uuid}/{subject}".encode()).hexdigest()[:8], 16)
    return "holdout" if h / 2**32 < holdout_fraction else "dev"


def _subject_evidence(task: TaskBundle, subject: str) -> tuple[str | None, str | None]:
    """(patch, sha) for a label subject: the gold patch or a run's shipped patch."""
    if subject == GOLD_LABEL:
        return task.fix_patch, task.fix_patch_sha256
    run = RunBundle(task.trajectories_dir / subject)
    if not run.root.is_dir():
        return None, None
    return run.agent_patch, run.agent_patch_sha256


def cmd_quality_calibrate(args: argparse.Namespace) -> int:
    if args.emit_template:
        return _emit_template(args)
    seat = _seat(args)
    gates = {
        "min_kappa": args.min_kappa,
        "min_spearman": args.min_spearman,
        "min_pearson": args.min_pearson,
        "min_balanced_accuracy": args.min_balanced_accuracy,
        "min_subjects": args.min_subjects,
        "min_dev": args.min_dev,
        "min_holdout": args.min_holdout,
        "min_raters": args.min_raters,
    }
    base = _bundle_root(Path(args.delivery))
    labels_path = Path(args.labels)
    labels_sha = hashlib.sha256(labels_path.read_bytes()).hexdigest()

    # human ratings: (uuid, subject) -> dimension -> rater -> rating
    human: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
    forced_split: dict[tuple[str, str], str] = {}
    with labels_path.open(newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            dim = (row.get("dimension") or "").strip()
            rating = (row.get("rating") or "").strip()
            if not rating:
                continue
            try:
                value = int(rating)
            except ValueError:
                print(
                    f"quality-calibrate: line {n}: rating {rating!r} is not an integer"
                )
                return 2
            if value < 1 or value > 5:
                print(f"quality-calibrate: line {n}: rating {value} outside 1..5")
                return 2
            key = (row["task_uuid"].strip(), row["subject"].strip())
            rater = (row.get("rater") or "").strip()
            if not rater:
                print(f"quality-calibrate: line {n}: missing rater")
                return 2
            human.setdefault(key, {}).setdefault(dim, {})[rater] = value
            split = (row.get("split") or "").strip().lower()
            if split:
                if split not in ("dev", "holdout"):
                    print(
                        f"quality-calibrate: line {n}: split {split!r} not dev/holdout"
                    )
                    return 2
                if forced_split.setdefault(key, split) != split:
                    print(f"quality-calibrate: line {n}: conflicting split for {key}")
                    return 2

    verdict_root = Path(args.verdicts)
    manifests: set[str] = set()
    prompts: set[str] = set()
    judge_scores: dict[tuple[str, str], float] = {}
    judge_dims: dict[tuple[str, str], dict[str, bool]] = {}
    missing: list[str] = []
    stale: list[str] = []
    incomplete: list[str] = []
    dims_by_key: dict[tuple[str, str], list[str]] = {}
    weights: dict[str, float] = {}
    for key in sorted(human):
        uuid, subject = key
        task = TaskBundle(base / uuid)
        if not task.quality_path.is_file():
            missing.append(f"{uuid}/{subject}: no tests/quality.json")
            continue
        items = load_quality_items(task.quality_path)
        weights = {i.dimension: float(i.weight) for i in items}
        dims = [i.dimension for i in items]
        dims_by_key[key] = dims
        manifests.add(manifest_digest(task.quality_path))
        prompt = prompt_digest(task.quality_path)
        prompts.add(prompt)
        cells = human[key]
        if set(cells) != set(dims) or any(
            len(cells[d]) < gates["min_raters"] for d in dims
        ):
            incomplete.append(f"{uuid}/{subject}")
            continue
        patch, _sha = _subject_evidence(task, subject)
        if patch is None or not patch.strip():
            missing.append(f"{uuid}/{subject}: no patch")
            continue
        packet, _ = build_quality_packet(task.instruction, patch)
        expected = quality_fingerprint(
            judge_model=seat.model_id, evidence=evidence_digest(packet), prompt=prompt
        )
        records = read_records(verdict_file(quality_store(verdict_root, uuid), subject))
        if not records:
            missing.append(f"{uuid}/{subject}: no verdicts")
            continue
        if {str(r.get(FINGERPRINT_KEY)) for r in records} != {expected}:
            stale.append(f"{uuid}/{subject}")
            continue
        outcomes = replay_outcomes(records, items, seat.alias)
        if any(o.satisfied is None for o in outcomes):
            missing.append(f"{uuid}/{subject}: undecided verdicts")
            continue
        judge_dims[key] = {o.item.dimension: bool(o.satisfied) for o in outcomes}
        total = sum(weights.values())
        judge_scores[key] = (
            sum(weights[o.item.dimension] for o in outcomes if o.satisfied) / total
        )

    complete = sorted(judge_scores)
    split = {
        k: forced_split.get(k) or subject_split(k[0], k[1], args.holdout_fraction)
        for k in complete
    }
    holdout = [k for k in complete if split[k] == "holdout"]
    dev = [k for k in complete if split[k] == "dev"]

    def consensus(k: tuple[str, str], dim: str) -> float:
        return sum(human[k][dim].values()) / len(human[k][dim])

    def human_score(k: tuple[str, str]) -> float:
        dims = dims_by_key[k]
        return sum(weights[d] * (consensus(k, d) - 1) / 4 for d in dims) / sum(
            weights[d] for d in dims
        )

    raters = sorted({r for k in complete for d in human[k] for r in human[k][d]})
    kappa = None
    if len(raters) >= 2:
        a: list[int] = []
        b: list[int] = []
        for k in complete:
            for d in human[k]:
                cell = human[k][d]
                if raters[0] in cell and raters[1] in cell:
                    a.append(cell[raters[0]])
                    b.append(cell[raters[1]])
        kappa = weighted_kappa(a, b) if a else None

    hx = [human_score(k) for k in holdout]
    jx = [judge_scores[k] for k in holdout]
    rho = spearman(jx, hx)
    r = pearson(jx, hx)
    per_dim: dict[str, Any] = {}
    for d in sorted(weights):
        pred = [judge_dims[k][d] for k in holdout]
        truth = [consensus(k, d) >= 4 for k in holdout]
        per_dim[d] = {
            "balanced_accuracy": balanced_accuracy(pred, truth),
            "n": len(pred),
            "both_classes": bool(truth) and any(truth) and not all(truth),
        }

    doc: dict[str, Any] = {
        "quality_version": QUALITY_VERSION,
        "model": seat.model_id,
        "judge": seat.alias,
        "prompt_digest": next(iter(prompts)) if len(prompts) == 1 else None,
        "manifest_digest": next(iter(manifests)) if len(manifests) == 1 else None,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "labels_sha256": labels_sha,
        "n_subjects": len(complete),
        "n_holdout": len(holdout),
        "n_dev": len(dev),
        "n_raters": len(raters),
        "holdout_fraction": args.holdout_fraction,
        "split": {f"{k[0]}/{k[1]}": v for k, v in split.items()},
        "missing_subjects": missing,
        "incomplete_subjects": incomplete,
        "stale_subjects": stale,
        "inter_rater_weighted_kappa": kappa,
        "holdout_spearman": rho,
        "holdout_pearson": r,
        "per_dimension": per_dim,
        "gates": gates,
        "passed": False,
    }
    if len(manifests) > 1 or len(prompts) > 1:
        doc["passed"], why = False, f"subjects span {len(manifests)} manifest digests"
    elif not manifests:
        doc["passed"], why = False, "no complete subject"
    else:
        doc["passed"], why = calibration_passes(doc, gates)
    doc["verdict_reason"] = why
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"quality-calibrate: {len(complete)} subjects ({len(dev)} dev, {len(holdout)} "
        f"holdout, {len(raters)} raters; {len(missing)} missing, {len(incomplete)} "
        f"incomplete, {len(stale)} stale) kappa={kappa} spearman={rho} pearson={r} "
        f"-> passed={doc['passed']} ({why}) {out}"
    )
    if args.materialize and doc["passed"]:
        data = out.read_bytes()
        n = sum(
            _write_if_changed(t.calibration_path, data)
            for t in _bundles(Path(args.delivery))
            if t.quality_path.is_file()
            and manifest_digest(t.quality_path) == doc["manifest_digest"]
        )
        print(f"quality-calibrate: materialized into {n} bundle(s)")
    elif args.materialize:
        print("quality-calibrate: not materialized (calibration did not pass)")
    return 0 if doc["passed"] else 1


# -- registration ----------------------------------------------------------------


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--judge-config",
        required=True,
        help="judge config JSON (judge_model decides the seat and the bridge)",
    )
    p.add_argument("--proxy", default="", help="override the bridge endpoint")


def register(sub: Any) -> None:
    p = sub.add_parser(
        "quality-init", help="materialize tests/quality.json into bundles"
    )
    p.add_argument("--task", default="")
    p.set_defaults(func=cmd_quality_init)

    p = sub.add_parser(
        "quality-judge", help="judge the shipped agent patch on the 7 quality items"
    )
    _common(p)
    p.add_argument("--task", required=True)
    p.add_argument("--models", default="")
    p.add_argument("--run", default="")
    p.add_argument("--out", default="")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--gold", action="store_true", help="judge solution/fix.patch instead of runs"
    )
    p.set_defaults(func=cmd_quality_judge)

    p = sub.add_parser(
        "quality-score", help="replay quality verdicts; write verifier/quality.json"
    )
    _common(p)
    p.add_argument("--task", required=True)
    p.add_argument("--models", default="")
    p.add_argument("--run", default="")
    p.add_argument("--verdicts", default="")
    p.add_argument("--write", action="store_true")
    p.add_argument(
        "--shadow",
        action="store_true",
        help="score without a passing calibration; score_quality is not published",
    )
    p.add_argument(
        "--gold", action="store_true", help="print the gold patch's report only"
    )
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_quality_score)

    p = sub.add_parser(
        "quality-calibrate", help="compare judge verdicts with human labels"
    )
    _common(p)
    p.add_argument(
        "--labels",
        default="",
        help="CSV: task_uuid,subject,dimension,rating,rater[,split]",
    )
    p.add_argument("--verdicts", default="", help="verdict store root")
    p.add_argument(
        "--out", required=True, help="judge_calibration.json (or template CSV)"
    )
    p.add_argument("--holdout-fraction", type=float, default=0.6)
    for k, v in DEFAULT_GATES.items():
        p.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    p.add_argument(
        "--materialize", action="store_true", help="copy into every bundle's tests/"
    )
    p.add_argument(
        "--emit-template", action="store_true", help="write a labels CSV skeleton"
    )
    p.set_defaults(func=cmd_quality_calibrate)
