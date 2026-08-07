"""Source-level tests: rubric wiring in run_eval.sh and pyproject.toml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def run_eval_src() -> str:
    return (REPO_ROOT / "run_eval.sh").read_text(encoding="utf-8")


class TestRunEvalWiring:
    def test_rubric_block_is_opt_in(self, run_eval_src: str):
        assert "RUBRIC_ENABLE:-0" in run_eval_src  # default OFF: zero behavior change

    def test_attach_runs_before_judge(self, run_eval_src: str):
        attach_pos = run_eval_src.index("multiswebench-rubric attach")
        judge_pos = run_eval_src.index("multiswebench-rubric judge")
        assert attach_pos < judge_pos

    def test_rubric_block_sits_between_harbor_and_stage_dataset(
        self, run_eval_src: str
    ):
        harbor_ok = run_eval_src.index('log "harbor: ok -> $HARBOR_OUT"')
        rubric = run_eval_src.index("multiswebench-rubric attach")
        stage = run_eval_src.index('stage_dataset "$DATASET_TAG"')
        assert harbor_ok < rubric < stage

    def test_judge_gets_run_base_for_git_patch(self, run_eval_src: str):
        judge_call = run_eval_src[run_eval_src.index("multiswebench-rubric judge") :]
        judge_call = judge_call[: judge_call.index("rrc=$?")]
        assert '--run-base "$RUN_BASE"' in judge_call

    def test_syntax_parses(self):
        import subprocess

        proc = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "run_eval.sh")], capture_output=True
        )
        assert proc.returncode == 0, proc.stderr.decode()


class TestEntryPoint:
    def test_pyproject_registers_cli(self):
        src = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(
            r'^multiswebench-rubric = "benchmarks\.multiswebench\.scripts\.rubric\.cli:main"$',
            src,
            re.MULTILINE,
        )

    def test_judge_config_committed_shape(self):
        import json

        cfg = json.loads(
            (REPO_ROOT / ".llm_config" / "rubric-judge.json").read_text(
                encoding="utf-8"
            )
        )
        assert cfg["base_url"] == "http://127.0.0.1:8765"  # host-side loopback
        assert "temperature" not in cfg  # Claude 5 rejects the parameter
        assert cfg["model"].startswith("anthropic/claude-sonnet")
