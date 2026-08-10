"""OpenAI-compatible FastAPI proxy backed by the Codex ChatGPT subscription.

Sibling of ``claude_code_bridge`` (port 8765) for the OpenAI Responses backend.
The harness points litellm's native Responses path at this bridge (an llm-config
whose ``base_url`` is ``http://<host>:8766`` and ``model`` is ``openai/<codex-model>``);
litellm ``POST``s ``{base_url}/responses``. The bridge:

  * swaps the stub ``Authorization`` for the real ChatGPT-subscription token from
    ``~/.codex/auth.json`` (auto-refreshed + written back by ``CodexCredentialProvider``);
  * adds the required Codex headers (``chatgpt-account-id``, ``originator``,
    ``OpenAI-Beta: responses=experimental``, User-Agent) -- no TLS-fingerprint
    impersonation (single account, same machine as the CLI; verified sufficient);
  * shapes the body for the fussy backend (force ``stream:true``/``store:false``,
    strip ``temperature``/``max_output_tokens``/``top_p``, fold system prompt);
  * ALWAYS streams upstream (the backend rejects ``stream:false``) and, when the
    CLIENT asked for non-streaming, aggregates the SSE into one Responses JSON;
  * classifies failures into the SAME ``aurora_bridge`` envelope the Claude bridge
    emits, so downstream backoff/pause logic is provider-agnostic.

Only ``POST /responses`` (+ ``/v1/responses`` alias) and ``GET /healthz`` are
served; every other path returns 404 with a hint (so a stray Anthropic-shaped
request -- e.g. assay's old 8766 default -- fails loudly instead of misrouting).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .credentials import CodexCredentialProvider, CredentialsError
from .errors import ClassifiedError, ErrorKind, classify_codex_error
from .shaping import shape_body
from .sse import SSEParser, aggregate


try:  # errors re-exports these from the claude bridge; keep a local fallback.
    from claude_code_bridge.errors import extract_retry_after  # noqa: F401
except Exception:  # pragma: no cover
    extract_retry_after = None  # type: ignore


_LOG = logging.getLogger(__name__)

UPSTREAM_DEFAULT = "https://chatgpt.com/backend-api/codex"
ORIGINATOR = "codex_cli_rs"
OPENAI_BETA = "responses=experimental"
USER_AGENT = "codex_cli_rs/0.147.0"

DEFAULT_REQUEST_TIMEOUT = 600.0
DEFAULT_READ_TIMEOUT = 180.0
DEFAULT_CONNECT_TIMEOUT = 30.0

# Inbound headers never forwarded upstream (we set our own auth + content).
STRIP_HEADERS_IN = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "content-length",
        "connection",
        "accept-encoding",
        "proxy-authorization",
    }
)
STRIP_HEADERS_OUT = frozenset(
    {"content-encoding", "transfer-encoding", "connection", "content-length"}
)


def _upstream_base() -> str:
    return os.environ.get("AURORA_CODEX_UPSTREAM", UPSTREAM_DEFAULT).rstrip("/")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _bridge_timeout() -> "httpx.Timeout":
    return httpx.Timeout(
        _env_float("AURORA_CODEX_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT),
        connect=_env_float("AURORA_CODEX_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
        read=_env_float("AURORA_CODEX_READ_TIMEOUT", DEFAULT_READ_TIMEOUT),
    )


def _max_inline_retries() -> int:
    try:
        return max(0, int(os.environ.get("AURORA_CODEX_MAX_INLINE_RETRIES", "")))
    except ValueError:
        return 3


def _max_inline_wait() -> int:
    try:
        return max(0, int(os.environ.get("AURORA_CODEX_MAX_INLINE_WAIT", "")))
    except ValueError:
        return 30


def _build_error_response(classified: ClassifiedError) -> JSONResponse:
    """Identical envelope to the Claude bridge, so downstream parsing matches."""
    headers: dict[str, str] = {"X-Aurora-Bridge-Error": classified.kind.value}
    if classified.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(1, classified.retry_after_seconds))
    if classified.reset_at_unix is not None:
        headers["X-Aurora-Reset-At"] = f"{classified.reset_at_unix:.0f}"
    body = {
        "type": "error",
        "error": {
            "type": classified.raw_error_type or "rate_limit_error",
            "message": classified.message,
        },
        "aurora_bridge": {
            "kind": classified.kind.value,
            "retry_after_seconds": classified.retry_after_seconds,
            "reset_at_unix": classified.reset_at_unix,
            "request_id": classified.request_id,
        },
    }
    return JSONResponse(body, status_code=classified.status_code, headers=headers)


def _forward_headers(
    request_headers: Any, token: str, account_id: Optional[str]
) -> dict[str, str]:
    fwd: dict[str, str] = {}
    for k, v in request_headers.items():
        if k.lower() in STRIP_HEADERS_IN:
            continue
        fwd[k] = v
    fwd["Authorization"] = f"Bearer {token}"
    fwd["content-type"] = "application/json"
    fwd.setdefault("Accept", "text/event-stream")
    fwd["originator"] = ORIGINATOR
    fwd["OpenAI-Beta"] = OPENAI_BETA
    fwd.setdefault("User-Agent", USER_AGENT)
    if account_id:
        fwd["chatgpt-account-id"] = account_id
    return fwd


def _client_wants_stream(body: dict[str, Any]) -> bool:
    return body.get("stream") is True


async def _open_upstream(
    client: httpx.AsyncClient,
    url: str,
    raw_body: bytes,
    headers: dict[str, str],
):
    cm = client.stream("POST", url, content=raw_body, headers=headers)
    upstream = await cm.__aenter__()
    return cm, upstream


def build_app(provider: Optional[CodexCredentialProvider] = None) -> FastAPI:
    app = FastAPI(title="Codex OAuth Bridge (aurora)", version="1.0.0")
    prov = provider if provider is not None else CodexCredentialProvider()
    fold = os.environ.get("AURORA_CODEX_NO_FOLD") != "1"

    @app.get("/healthz")
    async def healthz():
        try:
            token = await asyncio.to_thread(prov.get_access_token)
            account_id = await asyncio.to_thread(prov.get_account_id)
        except CredentialsError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        return {
            "ok": True,
            "token_prefix": token[:12] + "...",
            "account_id": account_id,
        }

    async def _handle_responses(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body is not a JSON object")
        except ValueError as e:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"bad JSON body: {e}",
                    },
                },
                status_code=400,
            )

        wants_stream = _client_wants_stream(body)
        # Shape exactly once (fold is not idempotent), before the retry loop.
        shaped = shape_body(body, fold_instructions=fold)
        shaped_bytes = json.dumps(shaped).encode("utf-8")
        url = f"{_upstream_base()}/responses"

        return await _forward_with_retry(
            prov, url, shaped_bytes, request.headers, wants_stream
        )

    @app.post("/responses")
    async def responses(request: Request) -> Response:
        return await _handle_responses(request)

    @app.post("/v1/responses")
    async def responses_v1(request: Request) -> Response:
        return await _handle_responses(request)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def not_found(path: str, request: Request) -> Response:
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "not_found",
                    "message": (
                        f"codex_bridge serves only POST /responses and GET /healthz; "
                        f"got {request.method} /{path}. If you meant the Anthropic "
                        f"bridge, use port 8765 (/v1/messages)."
                    ),
                },
            },
            status_code=404,
        )

    return app


async def _forward_with_retry(
    prov: CodexCredentialProvider,
    url: str,
    raw_body: bytes,
    headers_in: Any,
    wants_stream: bool,
) -> Response:
    max_retries = _max_inline_retries()
    max_wait = _max_inline_wait()
    attempt = 0
    did_force_reload = False

    while True:
        try:
            token = await asyncio.to_thread(prov.get_access_token)
            account_id = await asyncio.to_thread(prov.get_account_id)
        except CredentialsError as e:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": str(e)},
                    "aurora_bridge": {"kind": "credentials_unavailable"},
                },
                status_code=401,
            )

        fwd_headers = _forward_headers(headers_in, token, account_id)
        client = httpx.AsyncClient(timeout=_bridge_timeout())
        try:
            cm, upstream = await _open_upstream(client, url, raw_body, fwd_headers)
        except httpx.HTTPError as e:
            await client.aclose()
            _LOG.warning("codex upstream open error: %s", e)
            if attempt >= max_retries:
                return JSONResponse(
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e)},
                        "aurora_bridge": {"kind": "network_error"},
                    },
                    status_code=502,
                )
            attempt += 1
            await asyncio.sleep(min(2**attempt, max_wait))
            continue

        # 2xx: stream through (client streaming) or aggregate (client non-stream).
        if 200 <= upstream.status_code < 300:
            if wants_stream:
                return _streaming_passthrough(cm, upstream, client)
            result = await _aggregate_response(cm, upstream, client)
            if isinstance(result, Response):
                return result
            classified, retry_ok = result
            # Aggregation hit a failure/truncation.
            if retry_ok and classified.kind.is_retryable and attempt < max_retries:
                attempt += 1
                await asyncio.sleep(
                    min(classified.retry_after_seconds or 2**attempt, max_wait)
                )
                continue
            _apply_classification(prov, classified)
            return _build_error_response(classified)

        # Non-2xx: drain for classification, then retry / envelope.
        body_bytes = b""
        try:
            async for chunk in upstream.aiter_bytes():
                body_bytes += chunk
                if len(body_bytes) > 65536:
                    break
        finally:
            await cm.__aexit__(None, None, None)
            await client.aclose()

        classified = classify_codex_error(
            upstream.status_code,
            body_bytes,
            upstream.headers,
            request_id=upstream.headers.get("x-request-id"),
        )
        _LOG.info(
            "codex upstream error: status=%d kind=%s",
            upstream.status_code,
            classified.kind.value,
        )
        _apply_classification(prov, classified)

        # 401 once -> force reload creds and retry (token may have rotated).
        if classified.kind == ErrorKind.OAUTH_TOKEN_INVALID and not did_force_reload:
            did_force_reload = True
            prov.force_reload()
            attempt += 1
            continue
        if classified.kind.is_retryable and attempt < max_retries:
            wait = classified.retry_after_seconds or (2**attempt)
            if wait <= max_wait:
                attempt += 1
                await asyncio.sleep(wait)
                continue
        return _build_error_response(classified)


def _apply_classification(
    prov: CodexCredentialProvider, classified: ClassifiedError
) -> None:
    if classified.kind == ErrorKind.OAUTH_TOKEN_INVALID:
        prov.force_reload()


def _streaming_passthrough(cm, upstream, client) -> StreamingResponse:
    async def event_stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await cm.__aexit__(None, None, None)
            await client.aclose()

    headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in STRIP_HEADERS_OUT
    }
    # The backend leaves content-type empty; set the correct SSE type for clients.
    return StreamingResponse(
        event_stream(),
        status_code=upstream.status_code,
        headers=headers,
        media_type="text/event-stream",
    )


async def _aggregate_response(cm, upstream, client):
    """Consume the upstream SSE and return a JSONResponse (success) OR a
    ``(ClassifiedError, retry_ok)`` tuple (failure/truncation)."""
    parser = SSEParser()
    events = []
    try:
        async for chunk in upstream.aiter_bytes():
            events.extend(parser.feed(chunk))
        events.extend(parser.close())
    except httpx.HTTPError as e:
        _LOG.warning("codex stream read error: %s", e)
        return (
            ClassifiedError(
                kind=ErrorKind.UPSTREAM_5XX,
                status_code=502,
                retry_after_seconds=None,
                reset_at_unix=None,
                message=f"stream read error: {e}",
                raw_error_type="api_error",
            ),
            True,
        )
    finally:
        await cm.__aexit__(None, None, None)
        await client.aclose()

    agg = aggregate(events)
    if agg.ok and agg.response is not None:
        return JSONResponse(agg.response, status_code=200)
    if agg.kind == "failed":
        from .errors import classify_failed_event

        return (classify_failed_event(agg.error or {}), True)
    # truncated: no terminal event
    return (
        ClassifiedError(
            kind=ErrorKind.UPSTREAM_5XX,
            status_code=502,
            retry_after_seconds=None,
            reset_at_unix=None,
            message="codex stream ended with no terminal event",
            raw_error_type="incomplete_stream",
        ),
        True,
    )


app = build_app()
