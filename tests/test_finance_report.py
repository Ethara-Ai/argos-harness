"""Pins for the Finance API usage report.

run_eval.sh calls this once a dataset has finished and treats any failure as a
warning, on the rule that billing telemetry must never void a completed run.
That makes the module quiet on error by design, so the behaviours worth pinning
are the ones a silent wrong answer would hide: notional pricing for
subscription runs whose recorded cost is 0, provider-prefix stripping that
keeps trajectory and judge model names joinable, and a transport failure
surfacing as a return value rather than an exception.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "finance_report.py"


def _load() -> ModuleType:
    # scripts/ is not a package and the module inserts its own directory on
    # sys.path for the claude_account import, so it is loaded by location.
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("finance_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fr = _load()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in (
        "FINANCE_PROJECT_ID",
        "FINANCE_PROJECT_TYPE",
        "FINANCE_TEAM_TYPE",
        "FINANCE_BUDGET_TYPE",
        "FINANCE_RFP_SUB_TYPE",
        "FINANCE_PRODUCTION_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fr, "_ENV_FILE_CACHE", {})


def _run_dir(tmp_path: Path, *, usage: dict, cost=0, model=None, name="run_1") -> Path:
    run = tmp_path / "opus-5" / name
    run.mkdir(parents=True)
    record = {"metrics": {"accumulated_token_usage": usage, "accumulated_cost": cost}}
    run.joinpath("output.jsonl").write_text(json.dumps(record) + "\n")
    if model:
        run.joinpath("metadata.json").write_text(json.dumps({"model": model}))
    return run


def _verdicts(bundle: Path, run_name: str, records: list[dict]) -> None:
    d = bundle / "trajectories" / "opus-5" / run_name / "verifier"
    d.mkdir(parents=True)
    d.joinpath("verdicts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )


class TestNotionalCost:
    def test_unknown_model_is_free(self):
        assert fr._notional_cost_usd("gpt-4", {"prompt_tokens": 1_000_000}) == 0.0

    def test_uncached_input_and_output_are_billed_per_mtok(self):
        # opus: $5/MTok in, $25/MTok out.
        cost = fr._notional_cost_usd(
            "claude-opus-5",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        assert cost == pytest.approx(30.0)

    def test_cache_reads_bill_at_one_tenth_of_input(self):
        cost = fr._notional_cost_usd(
            "claude-opus-5",
            {"prompt_tokens": 1_000_000, "cache_read_tokens": 1_000_000},
        )
        assert cost == pytest.approx(0.5)

    def test_cache_writes_bill_at_one_and_a_quarter_input(self):
        cost = fr._notional_cost_usd(
            "claude-opus-5",
            {"prompt_tokens": 1_000_000, "cache_write_tokens": 1_000_000},
        )
        assert cost == pytest.approx(6.25)

    def test_cached_tokens_are_not_billed_twice(self):
        # litellm folds cache reads/writes into prompt_tokens; each tier must
        # be charged once, so a fully cached prompt costs only the cache rate.
        both = fr._notional_cost_usd(
            "claude-opus-5",
            {
                "prompt_tokens": 1_000_000,
                "cache_read_tokens": 600_000,
                "cache_write_tokens": 400_000,
            },
        )
        assert both == pytest.approx(600_000 * 0.5 / 1e6 + 400_000 * 6.25 / 1e6)

    def test_first_matching_price_tier_wins(self):
        assert fr._notional_cost_usd(
            "claude-sonnet-5", {"completion_tokens": 1_000_000}
        ) == pytest.approx(15.0)
        assert fr._notional_cost_usd(
            "claude-haiku-4", {"completion_tokens": 1_000_000}
        ) == pytest.approx(5.0)

    def test_missing_and_none_counts_are_treated_as_zero(self):
        assert fr._notional_cost_usd("claude-opus-5", {}) == 0.0
        assert fr._notional_cost_usd("claude-opus-5", {"prompt_tokens": None}) == 0.0


class TestModelName:
    def test_metadata_wins_and_provider_prefix_is_stripped(self, tmp_path):
        # Judge lines key on the bare id; keeping "anthropic/" here would split
        # the same model into two rows on the invoice.
        run = _run_dir(tmp_path, usage={}, model="anthropic/claude-opus-5")
        assert fr._model_name(run, {}) == "claude-opus-5"

    def test_falls_back_to_first_cost_entry(self, tmp_path):
        run = _run_dir(tmp_path, usage={})
        assert fr._model_name(run, {"costs": [{"model": "claude-sonnet-5"}]}) == (
            "claude-sonnet-5"
        )

    def test_unknown_when_nothing_identifies_the_model(self, tmp_path):
        run = _run_dir(tmp_path, usage={})
        assert fr._model_name(run, {}) == "unknown"

    def test_corrupt_metadata_does_not_raise(self, tmp_path):
        run = _run_dir(tmp_path, usage={})
        run.joinpath("metadata.json").write_text("{not json")
        assert fr._model_name(run, {"model_name": "fallback"}) == "fallback"


class TestLoadFirstRecord:
    def test_reads_the_first_non_blank_line(self, tmp_path):
        p = tmp_path / "output.jsonl"
        p.write_text('\n\n{"a": 1}\n{"a": 2}\n')
        assert fr._load_first_record(p) == {"a": 1}

    def test_empty_file_is_an_empty_record(self, tmp_path):
        p = tmp_path / "output.jsonl"
        p.write_text("")
        assert fr._load_first_record(p) == {}


class TestJudgeLines:
    def test_missing_bundle_yields_no_lines(self, tmp_path):
        assert fr._judge_lines(tmp_path / "nope", "run_1") == []

    def test_usage_is_summed_per_model(self, tmp_path):
        _verdicts(
            tmp_path,
            "run_1",
            [
                {
                    "model": "gpt-5.6-sol",
                    "usage": {"input_tokens": 10, "cost_usd": 0.1},
                },
                {"model": "gpt-5.6-sol", "usage": {"input_tokens": 5, "cost_usd": 0.2}},
            ],
        )
        (line,) = fr._judge_lines(tmp_path, "run_1")
        assert line["model_name"] == "gpt-5.6-sol"
        assert line["judge_input_tokens"] == 15
        assert line["judge_cost_usd"] == pytest.approx(0.3)

    def test_distinct_models_get_distinct_lines(self, tmp_path):
        _verdicts(
            tmp_path,
            "run_1",
            [
                {"model": "gpt-5.6-sol", "usage": {"input_tokens": 1}},
                {"model": "claude-opus-5", "usage": {"input_tokens": 2}},
            ],
        )
        assert {ln["model_name"] for ln in fr._judge_lines(tmp_path, "run_1")} == {
            "gpt-5.6-sol",
            "claude-opus-5",
        }

    def test_verdicts_without_cost_fall_back_to_notional_price(self, tmp_path):
        # Verdicts written before usage instrumentation carry no cost_usd; the
        # line must still price rather than report the run as free.
        _verdicts(
            tmp_path,
            "run_1",
            [{"model": "claude-opus-5", "usage": {"output_tokens": 1_000_000}}],
        )
        (line,) = fr._judge_lines(tmp_path, "run_1")
        assert line["judge_cost_usd"] == pytest.approx(25.0)

    def test_records_without_a_model_are_skipped(self, tmp_path):
        _verdicts(tmp_path, "run_1", [{"usage": {"input_tokens": 99}}])
        assert fr._judge_lines(tmp_path, "run_1") == []

    def test_only_the_requested_run_is_summed(self, tmp_path):
        _verdicts(tmp_path, "run_1", [{"model": "m", "usage": {"input_tokens": 1}}])
        _verdicts(tmp_path, "run_2", [{"model": "m", "usage": {"input_tokens": 500}}])
        (line,) = fr._judge_lines(tmp_path, "run_1")
        assert line["judge_input_tokens"] == 1

    def test_corrupt_verdicts_file_does_not_raise(self, tmp_path):
        d = tmp_path / "trajectories" / "opus-5" / "run_1" / "verifier"
        d.mkdir(parents=True)
        d.joinpath("verdicts.jsonl").write_text("{not json\n")
        assert fr._judge_lines(tmp_path, "run_1") == []


class TestBuildPayload:
    def test_token_counts_and_ids_are_carried_through(self, tmp_path):
        run = _run_dir(
            tmp_path,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cache_read_tokens": 5,
                "cache_write_tokens": 3,
            },
            cost=1.5,
            model="anthropic/claude-opus-5",
        )
        p = fr.build_payload(run, "uuid-1", "org__repo-42", tmp_path / "none", "sub-9")
        assert p["task_id"] == "org__repo-42"
        assert p["trajectory_id"] == "uuid-1/opus-5/run_1"
        assert p["subscription_id"] == "sub-9"
        assert p["model_name"] == "claude-opus-5"
        assert p["trajectory_input_tokens"] == 100
        assert p["trajectory_output_tokens"] == 20
        assert p["trajectory_input_cache_tokens"] == 5
        assert p["trajectory_output_cache_tokens"] == 3

    def test_recorded_cost_is_preferred_over_notional(self, tmp_path):
        run = _run_dir(
            tmp_path,
            usage={"completion_tokens": 1_000_000},
            cost=0.25,
            model="claude-opus-5",
        )
        p = fr.build_payload(run, "u", "i", tmp_path / "none", "s")
        assert p["trajectory_cost_usd"] == 0.25

    def test_zero_cost_subscription_run_is_priced_notionally(self, tmp_path):
        # The whole reason notional pricing exists: subscription runs record
        # accumulated_cost == 0 and would otherwise bill as free.
        run = _run_dir(
            tmp_path,
            usage={"completion_tokens": 1_000_000},
            cost=0,
            model="claude-opus-5",
        )
        p = fr.build_payload(run, "u", "i", tmp_path / "none", "s")
        assert p["trajectory_cost_usd"] == pytest.approx(25.0)

    def test_env_defaults_apply(self, tmp_path):
        run = _run_dir(tmp_path, usage={})
        p = fr.build_payload(run, "u", "i", tmp_path / "none", "s")
        assert p["project_id"] == "PRJ-UNSET"
        assert p["project_type"] == "Technical"
        assert p["team_type"] == "Projects"
        assert p["budget_type"] == "RFP"
        assert p["rfp_sub_type"] == "Testing"
        assert p["production_mode"] == "Singlephase"
        assert p["is_phase_based"] is False

    def test_multiphase_sets_is_phase_based(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FINANCE_PRODUCTION_MODE", "Multiphase")
        run = _run_dir(tmp_path, usage={})
        p = fr.build_payload(run, "u", "i", tmp_path / "none", "s")
        assert p["is_phase_based"] is True

    def test_payload_is_json_serialisable(self, tmp_path):
        run = _run_dir(tmp_path, usage={"prompt_tokens": 1}, model="claude-opus-5")
        p = fr.build_payload(run, "u", "i", tmp_path / "none", "s")
        assert json.loads(json.dumps(p))["trajectory_id"] == "u/opus-5/run_1"


class TestPost:
    def _resp(self, ok=True, status=200, text="{}"):
        return type("R", (), {"ok": ok, "status_code": status, "text": text})()

    def test_transport_failure_returns_false_and_does_not_raise(self, monkeypatch):
        # run_eval.sh treats a finance failure as a warning; an exception here
        # would escape that contract and void a finished dataset.
        def boom(*a, **k):
            raise fr.requests.RequestException("no route")

        monkeypatch.setattr(fr.requests, "post", boom)
        ok, detail = fr._post({}, "https://api.example.com")
        assert ok is False
        assert "request failed" in detail

    def test_http_error_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(
            fr.requests, "post", lambda *a, **k: self._resp(False, 401, "denied")
        )
        ok, detail = fr._post({}, "https://api.example.com")
        assert ok is False
        assert "401" in detail

    def test_endpoint_path_is_appended_once(self, monkeypatch):
        seen = {}

        def capture(url, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers", {})
            return self._resp()

        monkeypatch.setattr(fr.requests, "post", capture)
        fr._post({}, "https://api.example.com/api/v1/")
        assert seen["url"] == f"https://api.example.com/api/v1/{fr.ENDPOINT_PATH}"

    def test_bearer_token_becomes_an_authorization_header(self, monkeypatch):
        seen = {}
        monkeypatch.setenv("FINANCE_API_TOKEN", "tok123")
        monkeypatch.delenv("FINANCE_AUTH_HEADER", raising=False)
        monkeypatch.setattr(
            fr.requests,
            "post",
            lambda url, **kw: (seen.update(kw), self._resp())[1],
        )
        fr._post({}, "https://api.example.com")
        assert seen["headers"]["Authorization"] == "Bearer tok123"

    def test_raw_auth_header_takes_precedence_over_the_token(self, monkeypatch):
        seen = {}
        monkeypatch.setenv("FINANCE_API_TOKEN", "tok123")
        monkeypatch.setenv("FINANCE_AUTH_HEADER", "X-Api-Key: secret")
        monkeypatch.setattr(
            fr.requests,
            "post",
            lambda url, **kw: (seen.update(kw), self._resp())[1],
        )
        fr._post({}, "https://api.example.com")
        assert seen["headers"]["X-Api-Key"] == "secret"
        assert "Authorization" not in seen["headers"]


class TestEnv:
    def test_environment_beats_the_env_file(self, monkeypatch):
        monkeypatch.setattr(fr, "_ENV_FILE_CACHE", {"FINANCE_PROJECT_ID": "from-file"})
        monkeypatch.setenv("FINANCE_PROJECT_ID", "from-env")
        assert fr._env("FINANCE_PROJECT_ID") == "from-env"

    def test_env_file_is_used_when_the_environment_is_unset(self, monkeypatch):
        monkeypatch.setattr(fr, "_ENV_FILE_CACHE", {"FINANCE_PROJECT_ID": "from-file"})
        monkeypatch.delenv("FINANCE_PROJECT_ID", raising=False)
        assert fr._env("FINANCE_PROJECT_ID") == "from-file"

    def test_default_is_returned_and_values_are_stripped(self, monkeypatch):
        monkeypatch.setattr(fr, "_ENV_FILE_CACHE", {})
        assert fr._env("FINANCE_MISSING", "fallback") == "fallback"
        monkeypatch.setenv("FINANCE_PROJECT_ID", "  padded  ")
        assert fr._env("FINANCE_PROJECT_ID") == "padded"
