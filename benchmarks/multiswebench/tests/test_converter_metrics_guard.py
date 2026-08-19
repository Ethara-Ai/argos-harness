"""Pins for the Class-B metrics-attribution guard in build_atif_trajectory.

Delivered bundle 0ae03990 (batch 189b37e, 2026-08-04) shipped three
trajectories whose every step reads prompt_tokens=0 / completion_tokens=0
because the token_usages -> llm_response_id join was lost upstream. The guard
detects that signature (total join loss) and is STRICT BY DEFAULT: the
conversion fails so a corrupted-but-plausible bundle can never ship silently.
HARBOR_STRICT_METRICS=0 is the explicit opt-out (warn instead of fail), for
debugging or deliberate reprocessing of known-broken historical runs. Partial
join coverage is expected upstream behavior and must stay silent in any mode.
"""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks.multiswebench.scripts.harbor.converter import build_atif_trajectory


def _action_event(rid: str, tcid: str) -> dict[str, Any]:
    return {
        "kind": "ActionEvent",
        "timestamp": "2026-08-18T12:00:00.000000",
        "thought": [{"type": "text", "text": "thinking about it"}],
        "tool_call_id": tcid,
        "tool_name": "terminal",
        "tool_call": {"arguments": {"command": "ls"}},
        "llm_response_id": rid,
    }


def _token_usage(rid: str) -> dict[str, Any]:
    return {
        "response_id": rid,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_read_tokens": 0,
        "reasoning_tokens": 5,
        "cache_write_tokens": 0,
    }


def _build(history: list[dict[str, Any]], token_usages: list[dict[str, Any]]):
    return build_atif_trajectory(
        history=history,
        agent_tag="openhands",
        model_name_full="anthropic/claude-opus-4-8",
        model_name_bare="claude-opus-4-8",
        session_id="s-1",
        accumulated_token_usage={},
        token_usages=token_usages,
    )


def test_total_join_loss_strict_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HARBOR_STRICT_METRICS", raising=False)
    history = [_action_event("r1", "t1"), _action_event("r2", "t2")]
    with pytest.raises(ValueError, match="metrics-attribution"):
        _build(history, token_usages=[])


def test_total_join_loss_explicit_strict_still_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HARBOR_STRICT_METRICS", "1")
    history = [_action_event("r1", "t1")]
    with pytest.raises(ValueError, match="metrics-attribution"):
        _build(history, token_usages=[])


def test_total_join_loss_optout_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("HARBOR_STRICT_METRICS", "0")
    history = [_action_event("r1", "t1"), _action_event("r2", "t2")]
    traj = _build(history, token_usages=[])
    err = capsys.readouterr().err
    assert "WARN metrics-attribution" in err
    assert "0 of 2 action events" in err
    # conversion still succeeds, with the zeroed metrics Class B produces
    steps = [s for s in traj["steps"] if s["source"] == "agent"]
    assert steps and all(s["metrics"]["prompt_tokens"] == 0 for s in steps)


def test_healthy_join_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("HARBOR_STRICT_METRICS", raising=False)
    history = [_action_event("r1", "t1")]
    traj = _build(history, token_usages=[_token_usage("r1")])
    assert "metrics-attribution" not in capsys.readouterr().err
    steps = [s for s in traj["steps"] if s["source"] == "agent"]
    assert steps[0]["metrics"]["prompt_tokens"] == 100


def test_partial_join_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # Signature-only / unmatched turns are expected upstream behavior; a
    # partial join must not trigger the guard, even in strict-default mode.
    monkeypatch.delenv("HARBOR_STRICT_METRICS", raising=False)
    history = [_action_event("r1", "t1"), _action_event("r2", "t2")]
    _build(history, token_usages=[_token_usage("r1")])
    assert "metrics-attribution" not in capsys.readouterr().err


def test_no_actions_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("HARBOR_STRICT_METRICS", raising=False)
    _build([], token_usages=[])
    assert "metrics-attribution" not in capsys.readouterr().err
