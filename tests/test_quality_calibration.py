"""quality-calibrate enforces the protocol: subject counts, two ratings per
cell, valid ratings, current fingerprints, a per-subject stable split."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from assay import quality
from assay.cli import build_parser
from assay.quality import evidence_digest, quality_fingerprint
from assay.quality_cli import subject_split


MODEL_ID = "openai/responses/gpt-5.6-sol"
ALIAS = "gpt-5.6-sol"
DIMS = sorted(quality.QUALITY_DIMENSIONS)
YES = (
    "[[RATIONALE: r]]\n[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n[[EVIDENCE: e]]"
)
NO = YES.replace("Yes", "No")
HEADER = ("task_uuid", "subject", "dimension", "rating", "rater")


def _run(argv):
    args = build_parser().parse_args(argv)
    return args.func(args)


def _cfg(tmp_path: Path) -> Path:
    p = tmp_path / "j.json"
    p.write_text(json.dumps({"judge_model": MODEL_ID, "base_url": "x", "api_key": "y"}))
    return p


class Corpus:
    """N bundles with gold patches, judge verdicts and human labels that agree."""

    def __init__(self, tmp_path: Path, n: int, raters=("r1", "r2"), invert=False):
        self.delivery = tmp_path / "d"
        self.store = tmp_path / "verdicts"
        self.rows: list[tuple] = []
        self.uuids = [f"u{i:02d}" for i in range(n)]
        for i, uuid in enumerate(self.uuids):
            patch = f"diff --git a/f{i} b/f{i}\n+line {i}\n"
            b = self.delivery / uuid
            (b / "tests").mkdir(parents=True)
            (b / "solution").mkdir()
            (b / "solution" / "fix.patch").write_text(patch)
            (b / "instruction.md").write_text(f"task {i}")
            (b / "tests" / "quality.json").write_bytes(
                quality.MANIFEST_PATH.read_bytes()
            )
            yes = set(DIMS[: i % 8])
            self._verdicts(uuid, patch, yes)
            for d in DIMS:
                human = 5 if d in yes else 2
                if invert:
                    human = 7 - human
                for r in raters:
                    self.rows.append((uuid, "gold", d, str(human), r))

    def _verdicts(self, uuid: str, patch: str, yes: set[str], stale: bool = False):
        b = self.delivery / uuid
        packet, _ = quality.build_quality_packet(f"task {uuid}", patch)
        packet, _ = quality.build_quality_packet(
            (b / "instruction.md").read_text(), patch
        )
        fp = quality_fingerprint(
            judge_model=MODEL_ID,
            evidence=evidence_digest(packet),
            prompt=quality.prompt_digest(b / "tests" / "quality.json"),
        )
        if stale:
            fp = "0" * 16
        d = self.store / "quality" / uuid
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "member": ALIAS,
                    "model": ALIAS,
                    "item_id": it.id,
                    "completion": YES if it.dimension in yes else NO,
                    "error": "",
                    "quality_fingerprint": fp,
                }
            )
            for it in quality.load_quality_items(quality.MANIFEST_PATH)
        ]
        (d / "gold__quality.jsonl").write_text("\n".join(lines) + "\n")

    def labels(self, tmp_path: Path, rows=None, header=HEADER) -> Path:
        p = tmp_path / "labels.csv"
        with p.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows if rows is not None else self.rows)
        return p

    def calibrate(self, tmp_path: Path, labels: Path | None = None, extra=()):
        out = tmp_path / "judge_calibration.json"
        rc = _run(
            [
                "--delivery",
                str(self.delivery),
                "quality-calibrate",
                "--judge-config",
                str(_cfg(tmp_path)),
                "--labels",
                str(labels or self.labels(tmp_path)),
                "--verdicts",
                str(self.store),
                "--out",
                str(out),
                *extra,
            ]
        )
        return rc, json.loads(out.read_text())


def test_emit_template_lists_gold_cells_for_two_raters(tmp_path: Path):
    c = Corpus(tmp_path, 2)
    out = tmp_path / "template.csv"
    assert (
        _run(
            [
                "--delivery",
                str(c.delivery),
                "quality-calibrate",
                "--judge-config",
                str(_cfg(tmp_path)),
                "--out",
                str(out),
                "--emit-template",
            ]
        )
        == 0
    )
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 2 * 7 * 2
    assert {r["subject"] for r in rows} == {"gold"} and {r["rater"] for r in rows} == {
        "r1",
        "r2",
    }
    assert "split" in rows[0]


def test_full_protocol_passes_and_materializes(tmp_path: Path):
    c = Corpus(tmp_path, 60)
    rc, doc = c.calibrate(tmp_path, extra=["--materialize"])
    assert rc == 0 and doc["passed"] is True, doc["verdict_reason"]
    assert doc["n_subjects"] == 60 and doc["n_raters"] == 2
    assert (
        doc["n_dev"] >= 20
        and doc["n_holdout"] >= 30
        and doc["n_dev"] + doc["n_holdout"] == 60
    )
    assert doc["inter_rater_weighted_kappa"] == 1.0
    assert doc["holdout_spearman"] > 0.99 and doc["holdout_pearson"] > 0.99
    assert doc["manifest_digest"] == quality.manifest_digest(quality.MANIFEST_PATH)
    assert doc["gates"] == quality.DEFAULT_GATES
    assert len(doc["split"]) == 60 and doc["labels_sha256"]
    for uuid in c.uuids:
        cal = c.delivery / uuid / "tests" / "judge_calibration.json"
        assert json.loads(cal.read_text()) == doc
        ok, why = quality.calibration_check(
            cal,
            judge_model=MODEL_ID,
            prompt=quality.prompt_digest(quality.MANIFEST_PATH),
            manifest=quality.manifest_digest(quality.MANIFEST_PATH),
        )
        assert ok, why


def test_too_few_subjects_refused(tmp_path: Path):
    c = Corpus(tmp_path, 49)
    rc, doc = c.calibrate(tmp_path)
    assert rc == 1 and doc["passed"] is False
    assert "n_subjects=49" in doc["verdict_reason"]
    assert not (c.delivery / "u00" / "tests" / "judge_calibration.json").exists()


def test_one_rater_refused(tmp_path: Path):
    c = Corpus(tmp_path, 60, raters=("r1",))
    rc, doc = c.calibrate(tmp_path)
    assert rc == 1 and doc["passed"] is False
    assert doc["n_subjects"] == 0 and len(doc["incomplete_subjects"]) == 60


def test_missing_cell_makes_subject_incomplete(tmp_path: Path):
    c = Corpus(tmp_path, 60)
    rows = [
        r for r in c.rows if not (r[0] == "u00" and r[2] == "naming" and r[4] == "r2")
    ]
    rc, doc = c.calibrate(tmp_path, c.labels(tmp_path, rows))
    assert doc["incomplete_subjects"] == ["u00/gold"] and doc["n_subjects"] == 59
    assert rc == 0 and doc["passed"] is True


def test_stale_fingerprint_refused(tmp_path: Path):
    c = Corpus(tmp_path, 60)
    c._verdicts("u01", "diff --git a/f1 b/f1\n+line 1\n", set(DIMS[:1]), stale=True)
    rc, doc = c.calibrate(tmp_path)
    assert rc == 1 and doc["passed"] is False
    assert doc["stale_subjects"] == ["u01/gold"] and "stale" in doc["verdict_reason"]


def test_bad_rating_is_an_error(tmp_path: Path):
    c = Corpus(tmp_path, 60)
    rows = list(c.rows)
    rows[0] = rows[0][:3] + ("6",) + rows[0][4:]
    out = tmp_path / "cal.json"
    rc = _run(
        [
            "--delivery",
            str(c.delivery),
            "quality-calibrate",
            "--judge-config",
            str(_cfg(tmp_path)),
            "--labels",
            str(c.labels(tmp_path, rows)),
            "--verdicts",
            str(c.store),
            "--out",
            str(out),
        ]
    )
    assert rc == 2 and not out.exists()


def test_disagreement_fails(tmp_path: Path):
    c = Corpus(tmp_path, 60, invert=True)
    rc, doc = c.calibrate(tmp_path)
    assert rc == 1 and doc["passed"] is False
    assert doc["holdout_spearman"] < 0


def test_split_is_stable_per_subject_and_overridable(tmp_path: Path):
    assert subject_split("u", "gold", 0.6) == subject_split("u", "gold", 0.6)
    assert (
        subject_split("u", "gold", 0.0) == "dev"
        and subject_split("u", "gold", 1.0) == "holdout"
    )
    c = Corpus(tmp_path, 60)
    _, doc_all = c.calibrate(tmp_path)
    rows = [r for r in c.rows if r[0] != "u59"]
    _, doc_less = c.calibrate(tmp_path, c.labels(tmp_path, rows))
    for k, v in doc_less["split"].items():
        assert doc_all["split"][k] == v  # removing a subject moved nobody
    forced = [r + ("dev",) for r in c.rows]
    _, doc_forced = c.calibrate(
        tmp_path, c.labels(tmp_path, forced, HEADER + ("split",))
    )
    assert doc_forced["n_holdout"] == 0 and doc_forced["passed"] is False
    assert "n_holdout" in doc_forced["verdict_reason"]


def test_missing_judge_verdicts_are_reported_not_scored(tmp_path: Path):
    c = Corpus(tmp_path, 60)
    import shutil

    shutil.rmtree(c.store / "quality" / "u02")
    rc, doc = c.calibrate(tmp_path)
    assert (
        doc["missing_subjects"] == ["u02/gold: no verdicts"] and doc["n_subjects"] == 59
    )
    assert rc == 0 and doc["passed"] is True
