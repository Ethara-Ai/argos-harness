"""End to end, offline: quality-init, quality-judge (fake bridge), quality-score
in shadow and calibrated modes, and the interaction with ``score --write``."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

from assay import quality, quality_cli
from assay.cli import build_parser
from tests.test_quality_scoring import good_calibration


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "argos_bundles"
U943 = "150c282e-330b-492e-bcaf-1017bbfff2e8"
PATCH = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)
MODEL_ID = "openai/responses/gpt-5.6-sol"
YES = (
    "[[RATIONALE: r]]\n[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n[[EVIDENCE: e]]"
)


def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


@pytest.fixture
def delivery(tmp_path: Path) -> Path:
    d = tmp_path / "delivery"
    shutil.copytree(FIXTURES / U943, d / U943)
    shutil.copytree(FIXTURES / "verdicts" / U943, d / "verdicts" / U943)
    return d


@pytest.fixture
def judge_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "rubric-judge.json"
    p.write_text(
        json.dumps(
            {
                "judge_model": MODEL_ID,
                "base_url": "http://127.0.0.1:8766",
                "api_key": "stub",
            }
        )
    )
    return p


def _run_dir(delivery: Path) -> Path:
    return delivery / U943 / "trajectories" / "opus-4.8" / "run_1"


def _ship_patch(delivery: Path, patch: str = PATCH) -> None:
    art = _run_dir(delivery) / "artifacts"
    art.mkdir(exist_ok=True)
    (art / "agent.patch").write_text(patch, encoding="utf-8")


def _init(delivery: Path) -> None:
    assert _run(["--delivery", str(delivery), "quality-init", "--task", U943]) == 0


def _calibrate(delivery: Path) -> None:
    manifest = quality.manifest_digest(delivery / U943 / "tests" / "quality.json")
    prompt = quality.prompt_digest(delivery / U943 / "tests" / "quality.json")
    (delivery / U943 / "tests" / "judge_calibration.json").write_text(
        json.dumps(
            good_calibration(model_id=MODEL_ID, prompt=prompt, manifest=manifest)
        )
    )


def _fake_bridge(monkeypatch, answers: dict[str, bool], cite: bool = True):
    calls: list[tuple[str, str]] = []
    items = quality.load_quality_items(quality.MANIFEST_PATH)

    def fake(endpoint, model, system, question, evidence, deadline):
        calls.append((endpoint, model))
        assert system == quality.QUALITY_SYSTEM
        assert "Reference account" not in evidence
        assert PATCH in evidence
        item_id = next(i.id for i in items if i.criterion in question)
        yes = answers.get(item_id, True)
        ev = "[[EVIDENCE: a.py hunk 1]]" if cite else ""
        return (
            "[[RATIONALE: r]]\n"
            f"[[SATISFIED: {'Yes' if yes else 'No'}]]\n"
            "[[TRUNCATION_AFFECTED: No]]\n" + ev,
            "",
        )

    monkeypatch.setattr(quality_cli, "call_with_backoff", fake)
    return calls


def test_quality_init_materializes_manifest(delivery: Path):
    _init(delivery)
    assert (
        delivery / U943 / "tests" / "quality.json"
    ).read_bytes() == quality.MANIFEST_PATH.read_bytes()
    assert _run(["--delivery", str(delivery), "quality-init"]) == 0  # idempotent


def test_judge_and_score_refuse_without_bundle_manifest(
    delivery: Path, judge_cfg: Path, monkeypatch, capsys
):
    _ship_patch(delivery)
    calls = _fake_bridge(monkeypatch, {})
    base = ["--delivery", str(delivery)]
    assert (
        _run(base + ["quality-judge", "--judge-config", str(judge_cfg), "--task", U943])
        == 2
    )
    assert calls == []
    assert (
        _run(
            base
            + [
                "quality-score",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--shadow",
            ]
        )
        == 2
    )
    out = capsys.readouterr().out
    assert out.count("REFUSE") == 2 and "quality-init" in out


def test_judge_skips_runs_without_patch(
    delivery: Path, judge_cfg: Path, monkeypatch, capsys
):
    _init(delivery)
    calls = _fake_bridge(monkeypatch, {})
    rc = _run(
        [
            "--delivery",
            str(delivery),
            "quality-judge",
            "--judge-config",
            str(judge_cfg),
            "--task",
            U943,
        ]
    )
    assert rc == 0 and calls == []
    assert "evidence missing" in capsys.readouterr().out


def test_judge_then_score_shadow_then_calibrated(
    delivery: Path, judge_cfg: Path, monkeypatch
):
    _init(delivery)
    _ship_patch(delivery)
    calls = _fake_bridge(monkeypatch, {"Q2": False, "Q7": False})
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"

    judge_argv = base + [
        "quality-judge",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--out",
        str(store),
    ]
    assert _run(judge_argv) == 0
    assert len(calls) == 7
    assert calls[0] == ("http://127.0.0.1:8766/responses", "gpt-5.6-sol")
    vf = store / "quality" / U943 / "opus-4.8__run_1__quality.jsonl"
    assert not list((store / U943).glob("*quality*"))  # never beside rubric verdicts
    recs = [json.loads(line) for line in vf.read_text().splitlines()]
    assert [r["item_id"] for r in recs] == [f"Q{n}" for n in range(1, 8)]
    assert list(recs[0]) == [
        "member",
        "model",
        "item_id",
        "completion",
        "error",
        "quality_fingerprint",
    ]
    assert recs[0]["member"] == "gpt-5.6-sol"

    assert _run(judge_argv) == 0  # resume: nothing left to call
    assert len(calls) == 7

    score_argv = base + [
        "quality-score",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--verdicts",
        str(store),
        "--write",
    ]
    assert _run(score_argv) == 1  # uncalibrated -> refuse without --shadow
    vd = _run_dir(delivery) / "verifier"
    assert not (vd / "quality.json").exists()

    assert _run(score_argv + ["--shadow"]) == 0
    doc = json.loads((vd / "quality.json").read_text())
    assert doc["status"] == "scored" and doc["score"] == round(5 / 7, 4)
    assert doc["calibrated"] is False
    assert doc["version"]["manifest_digest"] == quality.manifest_digest(
        delivery / U943 / "tests" / "quality.json"
    )
    assert (vd / "quality_verdicts.jsonl").read_bytes() == vf.read_bytes()
    md = (vd / "final_score.md").read_text()
    assert md.count(quality.QUALITY_MD_START) == 1 and "shadow" in md
    assert md.startswith("# Score — opus-4.8/run_1")
    vr = json.loads((_run_dir(delivery) / "result.json").read_text())["verifier_result"]
    assert "score_quality" not in vr["scores"]
    assert vr["quality"]["status"] == "scored" and vr["quality"]["calibrated"] is False
    assert vr["quality"]["judge"] == MODEL_ID
    assert list(vr["assay"]) == ["alpha", "gate", "stratum_size", "judge", "status"]
    for k in ("score", "score_binary", "score_continuous_v2"):
        assert vr["scores"][k] == 0.0

    _calibrate(delivery)
    assert _run(score_argv) == 0
    vr = json.loads((_run_dir(delivery) / "result.json").read_text())["verifier_result"]
    assert vr["scores"]["score_quality"] == round(5 / 7, 4)
    assert vr["quality"]["calibrated"] is True
    assert "shadow" not in (vd / "final_score.md").read_text()


def test_bundle_manifest_is_the_authority(delivery: Path, judge_cfg: Path, monkeypatch):
    """Editing the bundle's tests/quality.json changes what is judged and
    invalidates verdicts; the package manifest is irrelevant afterwards."""
    _init(delivery)
    _ship_patch(delivery)
    _fake_bridge(monkeypatch, {})
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    judge_argv = base + [
        "quality-judge",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--out",
        str(store),
    ]
    score_argv = base + [
        "quality-score",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--verdicts",
        str(store),
        "--shadow",
        "--write",
    ]
    assert _run(judge_argv) == 0 and _run(score_argv) == 0
    before = json.loads((_run_dir(delivery) / "verifier" / "quality.json").read_text())

    mpath = delivery / U943 / "tests" / "quality.json"
    doc = json.loads(mpath.read_text())
    doc["items"][0]["criterion"] += " Also considers tests."
    mpath.write_text(json.dumps(doc, indent=2))
    assert _run(score_argv) == 1  # old verdicts are stale for the edited manifest
    after = json.loads((_run_dir(delivery) / "verifier" / "quality.json").read_text())
    assert after["status"] == "unjudged" and "fingerprint" in after["reasons"][0]
    assert after["version"]["manifest_digest"] != before["version"]["manifest_digest"]
    assert after["version"]["prompt_digest"] != before["version"]["prompt_digest"]


def test_rubric_score_rewrite_keeps_quality_block_and_number(
    delivery: Path, judge_cfg: Path, monkeypatch
):
    _init(delivery)
    _ship_patch(delivery)
    _fake_bridge(monkeypatch, {})
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    _calibrate(delivery)
    assert (
        _run(
            base
            + [
                "quality-judge",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--out",
                str(store),
            ]
        )
        == 0
    )
    assert (
        _run(
            base
            + [
                "quality-score",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--verdicts",
                str(store),
                "--write",
            ]
        )
        == 0
    )
    vd = _run_dir(delivery) / "verifier"
    before_md = (vd / "final_score.md").read_text()

    monkeypatch.setenv("ASSAY_COUNCIL", "sonnet-5=claude-sonnet-5")
    import assay.cli as ac
    import assay.judge as aj

    importlib.reload(aj)
    importlib.reload(ac)
    args = ac.build_parser().parse_args(
        base + ["score", "--task", U943, "--verdicts", str(store), "--write"]
    )
    assert args.func(args) == 0
    assert (vd / "final_score.md").read_text() == before_md
    vr = json.loads((_run_dir(delivery) / "result.json").read_text())["verifier_result"]
    assert vr["scores"]["score_quality"] == 1.0
    assert vr["quality"]["status"] == "scored"
    assert list(vr["assay"]) == ["alpha", "gate", "stratum_size", "judge", "status"]


def test_uncited_verdicts_make_run_unjudged(
    delivery: Path, judge_cfg: Path, monkeypatch
):
    _init(delivery)
    _ship_patch(delivery)
    _fake_bridge(monkeypatch, {}, cite=False)
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    assert (
        _run(
            base
            + [
                "quality-judge",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--out",
                str(store),
            ]
        )
        == 0
    )
    rc = _run(
        base
        + [
            "quality-score",
            "--judge-config",
            str(judge_cfg),
            "--task",
            U943,
            "--verdicts",
            str(store),
            "--write",
            "--shadow",
        ]
    )
    assert rc == 1
    doc = json.loads((_run_dir(delivery) / "verifier" / "quality.json").read_text())
    assert doc["status"] == "unjudged" and doc["score"] is None
    assert doc["counts"]["undecided"] == 7


def test_stale_records_are_dropped_on_rejudge(
    delivery: Path, judge_cfg: Path, monkeypatch
):
    _init(delivery)
    _ship_patch(delivery)
    calls = _fake_bridge(monkeypatch, {})
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    judge_argv = base + [
        "quality-judge",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--out",
        str(store),
    ]
    assert _run(judge_argv) == 0
    _ship_patch(delivery, PATCH + "+# more\n")  # patch changed -> new fingerprint
    monkeypatch.setattr(quality_cli, "call_with_backoff", lambda *a: (YES, ""))
    assert _run(judge_argv) == 0
    vf = store / "quality" / U943 / "opus-4.8__run_1__quality.jsonl"
    recs = [json.loads(line) for line in vf.read_text().splitlines()]
    assert len(recs) == 7 and len({r["quality_fingerprint"] for r in recs}) == 1
    assert len(calls) == 7  # first pass only; the second used the lambda


def test_gold_judge_and_score(delivery: Path, judge_cfg: Path, monkeypatch, capsys):
    _init(delivery)
    gold = (delivery / U943 / "solution" / "fix.patch").read_text()

    def fake(endpoint, model, system, question, evidence, deadline):
        assert gold[:200] in evidence
        return YES, ""

    monkeypatch.setattr(quality_cli, "call_with_backoff", fake)
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    assert (
        _run(
            base
            + [
                "quality-judge",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--out",
                str(store),
                "--gold",
            ]
        )
        == 0
    )
    assert (store / "quality" / U943 / "gold__quality.jsonl").is_file()
    assert (
        _run(
            base
            + [
                "quality-score",
                "--judge-config",
                str(judge_cfg),
                "--task",
                U943,
                "--verdicts",
                str(store),
                "--gold",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    doc = json.loads(printed[printed.index("{") :])
    assert doc["status"] == "scored" and doc["score"] == 1.0 and doc["run_id"] == "gold"
    assert not (_run_dir(delivery) / "verifier" / "quality.json").exists()


def test_resume_reasks_uncited_and_truncated_answers(
    delivery: Path, judge_cfg: Path, monkeypatch
):
    """An answer that cannot decide its item (no EVIDENCE tag, or cut off) is
    asked again on the next pass; the later usable answer wins at replay."""
    _init(delivery)
    _ship_patch(delivery)
    base = ["--delivery", str(delivery)]
    store = delivery / "verdicts"
    judge_argv = base + [
        "quality-judge",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--out",
        str(store),
    ]
    first = {
        "Q4": YES.replace("[[EVIDENCE: e]]", ""),
        "Q5": YES.replace("TRUNCATION_AFFECTED: No", "TRUNCATION_AFFECTED: Yes"),
    }
    items = quality.load_quality_items(quality.MANIFEST_PATH)
    calls: list[str] = []

    def fake(endpoint, model, system, question, evidence, deadline):
        item_id = next(i.id for i in items if i.criterion in question)
        calls.append(item_id)
        return first.get(item_id, YES), ""

    monkeypatch.setattr(quality_cli, "call_with_backoff", fake)
    assert _run(judge_argv) == 0 and len(calls) == 7
    score_argv = base + [
        "quality-score",
        "--judge-config",
        str(judge_cfg),
        "--task",
        U943,
        "--verdicts",
        str(store),
        "--shadow",
        "--write",
    ]
    assert _run(score_argv) == 1  # Q4 uncited, Q5 truncated -> unjudged

    first.clear()  # the judge now answers properly
    assert _run(judge_argv) == 0
    assert sorted(calls[7:]) == ["Q4", "Q5"]  # only the undecided items were re-asked
    assert _run(score_argv) == 0
    doc = json.loads((_run_dir(delivery) / "verifier" / "quality.json").read_text())
    assert doc["status"] == "scored" and doc["score"] == 1.0
    assert _run(judge_argv) == 0 and len(calls) == 9  # nothing left
