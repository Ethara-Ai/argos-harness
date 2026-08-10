"""The ONLY module that talks to the LLM bridge. Everything else is pure.

Transport contract: ``call()`` NEVER raises — it returns a ``JudgeCallResult``
where exactly one of ``text``/``error`` is None. Retry policy:

* retry (deterministic exponential backoff, no jitter, cap 60s):
  429 / 5xx / timeout / connection errors
* NEVER retry: 400 (context/bad request — same input fails identically),
  401/403 (auth — the whole batch will fail; caller aborts).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    # Claude 5 family models REJECT the temperature parameter outright
    # ("`temperature` is deprecated for this model", verified live 2026-08-06),
    # so None means "do not send it" and is the correct default for sonnet-5.
    temperature: float | None = None
    timeout: int = 600
    num_retries: int = 5
    max_tokens: int = 8_000
    usage_id: str = "rubric-judge"


def load_judge_config(path: str | Path) -> JudgeConfig:
    """Load a judge config JSON, ignoring unknown keys (mirrors the SDK LLM
    model's extra="ignore" so the same file style works everywhere).

    The transport model comes from "judge_model" when present, falling back to
    the legacy "model" key so older config files keep working unchanged."""
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"judge config {path} must be a JSON object")
    judge_model = str(raw.get("judge_model") or raw.get("model") or "").strip()
    if not judge_model:
        raise ValueError(
            f"judge config {path} missing required field 'judge_model' (or legacy 'model')"
        )
    for required in ("base_url", "api_key"):
        if not str(raw.get(required) or "").strip():
            raise ValueError(f"judge config {path} missing required field {required!r}")
    raw_temperature = raw.get("temperature")
    return JudgeConfig(
        model=judge_model,
        base_url=str(raw["base_url"]).rstrip("/"),
        api_key=str(raw["api_key"]),
        temperature=None if raw_temperature is None else float(raw_temperature),
        timeout=int(raw.get("timeout", 600)),
        num_retries=int(raw.get("num_retries", 5)),
        max_tokens=int(raw.get("max_tokens", 8_000)),
        usage_id=str(raw.get("usage_id", "rubric-judge")),
    )


@dataclass
class JudgeCallResult:
    text: str | None
    error: str | None
    error_kind: str | None = (
        None  # timeout|rate_limit|auth|connection|context_overflow|other
    )
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class JudgeTransport(Protocol):
    def call(self, system: str, user: str) -> JudgeCallResult: ...


_FATAL_KINDS = frozenset({"auth", "context_overflow"})


def _classify(exc: Exception) -> tuple[str, bool]:
    """→ (error_kind, retryable)."""
    import litellm

    if isinstance(exc, litellm.exceptions.ContextWindowExceededError):
        return "context_overflow", False
    if isinstance(
        exc,
        (
            litellm.exceptions.AuthenticationError,
            litellm.exceptions.PermissionDeniedError,
        ),
    ):
        return "auth", False
    if isinstance(exc, litellm.exceptions.BadRequestError):
        # 400s repeat identically; a context overflow sometimes surfaces as a
        # generic 400 — sniff the message so the caller's rebuild path triggers.
        if "context" in str(exc).lower() or "too long" in str(exc).lower():
            return "context_overflow", False
        return "other", False
    if isinstance(exc, litellm.exceptions.RateLimitError):
        return "rate_limit", True
    if isinstance(exc, (litellm.exceptions.Timeout,)):
        return "timeout", True
    if isinstance(
        exc,
        (
            litellm.exceptions.APIConnectionError,
            litellm.exceptions.ServiceUnavailableError,
            litellm.exceptions.InternalServerError,
        ),
    ):
        return "connection", True
    return "other", True


def _usage_from_response(response: Any) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    raw = getattr(response, "usage", None)
    if raw is not None:
        usage["prompt_tokens"] = int(getattr(raw, "prompt_tokens", 0) or 0)
        usage["completion_tokens"] = int(getattr(raw, "completion_tokens", 0) or 0)
    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response)
        usage["cost_usd"] = round(float(cost), 6) if cost else None
    except Exception:  # noqa: BLE001 — pricing is best-effort, never blocks grading
        usage["cost_usd"] = None
    return usage


class LiteLLMJudge:
    """JudgeTransport over litellm → the OAuth bridge (host-side loopback)."""

    def __init__(self, cfg: JudgeConfig, *, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep

    def call(self, system: str, user: str) -> JudgeCallResult:
        import litellm

        last_error = "no attempt made"
        last_kind: str | None = None
        attempts = 0
        for attempt in range(self.cfg.num_retries + 1):
            attempts = attempt + 1
            try:
                started = time.monotonic()
                kwargs: dict[str, Any] = {}
                if self.cfg.temperature is not None:
                    kwargs["temperature"] = self.cfg.temperature
                # non-streaming call; litellm's union return type includes the
                # stream wrapper, which never applies here
                response: Any = litellm.completion(
                    model=self.cfg.model,
                    api_base=self.cfg.base_url,
                    api_key=self.cfg.api_key,
                    timeout=self.cfg.timeout,
                    max_tokens=self.cfg.max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    **kwargs,
                )
                latency = round(time.monotonic() - started, 3)
                choice = (response.choices or [None])[0]
                text = getattr(getattr(choice, "message", None), "content", None)
                usage = _usage_from_response(response)
                usage["latency_s"] = latency
                return JudgeCallResult(
                    text=text if isinstance(text, str) else None,
                    error=None
                    if isinstance(text, str)
                    else "response had no text content",
                    error_kind=None if isinstance(text, str) else "other",
                    stop_reason=str(getattr(choice, "finish_reason", None) or ""),
                    usage=usage,
                    attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001 — transport must never raise
                kind, retryable = _classify(exc)
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                last_kind = kind
                if not retryable or attempt == self.cfg.num_retries:
                    break
                self._sleep(min(60.0, float(2 ** (attempt + 1))))
        return JudgeCallResult(
            text=None, error=last_error, error_kind=last_kind, attempts=attempts
        )

    def probe(self) -> JudgeCallResult:
        """Cheap reachability/auth check before a batch. One retry only."""
        probe_cfg = JudgeConfig(
            model=self.cfg.model,
            base_url=self.cfg.base_url,
            api_key=self.cfg.api_key,
            temperature=self.cfg.temperature,
            timeout=min(60, self.cfg.timeout),
            num_retries=1,
            max_tokens=16,
            usage_id=self.cfg.usage_id,
        )
        return LiteLLMJudge(probe_cfg, sleep=self._sleep).call(
            "Reply with the single word: pong", "ping"
        )
