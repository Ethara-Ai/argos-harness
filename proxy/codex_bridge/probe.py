"""Phase-0 probe: does the ChatGPT Codex backend accept a plain-httpx request
from this machine, with the token the Codex CLI already stored?

This is a GATE, not a component. It writes nothing, imports nothing from the
harness, and is safe to delete. It answers the questions the bridge design
depends on, empirically, before a line of bridge code is written:

  1. Does ``gpt-5.6-sol`` (the model the TL wants) work over this route?
  2. Does a plain httpx client (no TLS-fingerprint impersonation) get a 200,
     or a Cloudflare / attestation block?
  3. What is the SSE terminal-event vocabulary (response.completed /
     .incomplete / .failed / error / [DONE])?
  4. Which body params does the backend reject (temperature / max_output_tokens
     / top_p / store:true / non-stream)? -> finalizes the bridge's strip list.
  5. Does OpenAI rotate the refresh token on a refresh grant? -> decides the
     credential write-back merge strategy.

Run from the ``proxy/`` directory (or anywhere):  python -m codex_bridge.probe
Nothing secret is printed: tokens are shown only as short fingerprints.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx


# --- constants copied from the vendored SDK reference (auth/openai.py) --------
ISSUER = "https://auth.openai.com"
TOKEN_ENDPOINT = f"{ISSUER}/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

PRIMARY_MODEL = "gpt-5.6-sol"
# Informational only; the gate is PRIMARY_MODEL.
OTHER_MODELS = ["gpt-5.3-codex", "gpt-5.2-codex"]

# The SDK uses this exact short instruction string for subscription transport;
# the backend is known to reject long/complex `instructions`.
DEFAULT_INSTRUCTIONS = (
    "You are OpenHands agent, a helpful AI assistant that can interact "
    "with a computer to solve tasks."
)

# Desktop-mimic headers the SDK sends. These are FREE (static strings); we are
# NOT doing TLS fingerprinting.
BASE_HEADERS = {
    "originator": "codex_cli_rs",
    "OpenAI-Beta": "responses=experimental",
    "User-Agent": "codex_cli_rs/0.147.0 (probe)",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
}


def _fp(token: str) -> str:
    """A non-reversible fingerprint of a secret, for logs."""
    if not token:
        return "<empty>"
    return f"{token[:6]}...{token[-4:]} (len={len(token)})"


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT signature verification (the token came from
    our own disk, not a network attacker; we only read claims)."""
    try:
        _, payload, _ = token.split(".", 2)
        return json.loads(_b64url_decode(payload))
    except Exception as e:  # noqa: BLE001 - probe: never crash on a weird token
        print(f"  [warn] could not decode JWT claims: {e}")
        return {}


def _account_id(auth: dict[str, Any], access_token: str) -> Optional[str]:
    tokens = auth.get("tokens") or {}
    acct = tokens.get("account_id")
    if acct:
        return acct
    claims = _jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    return auth_info.get("chatgpt_account_id")


def _load_auth() -> dict[str, Any]:
    path = Path(
        os.environ.get(
            "AURORA_CODEX_AUTH_PATH", str(Path.home() / ".codex" / "auth.json")
        )
    ).expanduser()
    if not path.is_file():
        sys.exit(f"[probe] no auth.json at {path} — run `codex` and log in first")
    return json.loads(path.read_text(encoding="utf-8"))


def _exp_status(access_token: str) -> str:
    claims = _jwt_claims(access_token)
    exp = claims.get("exp")
    if not exp:
        return "no exp claim"
    dt = exp - time.time()
    return (
        f"exp in {dt / 60:.1f} min" if dt > 0 else f"EXPIRED {(-dt) / 60:.1f} min ago"
    )


# --- request construction -----------------------------------------------------
def _build_body(
    model: str,
    *,
    stream: bool = True,
    store: bool = False,
    instructions: str = DEFAULT_INSTRUCTIONS,
    extra: Optional[dict[str, Any]] = None,
    text: str = "Reply with exactly the word: pong",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        ],
        "store": store,
        "stream": stream,
        "tools": [],
        "tool_choice": "auto",
    }
    if extra:
        body.update(extra)
    return body


def _headers(access_token: str, account_id: Optional[str]) -> dict[str, str]:
    h = dict(BASE_HEADERS)
    h["Authorization"] = f"Bearer {access_token}"
    if account_id:
        h["chatgpt-account-id"] = account_id
    return h


def _summarize_sse(raw: str) -> dict[str, Any]:
    """Extract the sequence of event types + terminal payload facts from an SSE
    body, without printing the whole (possibly large) transcript."""
    event_types: list[str] = []
    saw_done = False
    terminal: dict[str, Any] = {}
    text_chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line == "data: [DONE]":
            saw_done = True
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        et = evt.get("type", "?")
        event_types.append(et)
        if et.endswith("output_text.delta") and "delta" in evt:
            text_chunks.append(str(evt["delta"]))
        if et in ("response.completed", "response.incomplete", "response.failed"):
            resp = evt.get("response", {})
            terminal = {
                "type": et,
                "status": resp.get("status"),
                "has_usage": bool(resp.get("usage")),
                "output_len": len(resp.get("output", []) or []),
                "error": resp.get("error") or evt.get("error"),
            }
        if et == "error":
            terminal = {"type": "error", "error": evt.get("error") or evt}
    # collapse consecutive duplicate event types for readability
    seq: list[str] = []
    for et in event_types:
        if not seq or seq[-1] != et:
            seq.append(et)
    return {
        "event_seq": seq,
        "saw_[DONE]": saw_done,
        "terminal": terminal,
        "text_preview": "".join(text_chunks)[:80],
    }


def _post(
    client: httpx.Client,
    access_token: str,
    account_id: Optional[str],
    body: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    print(f"\n=== {label} ===")
    print(
        f"  model={body['model']} stream={body['stream']} store={body['store']}"
        f" extra_keys={[k for k in body if k not in _BASELINE_KEYS]}"
    )
    try:
        r = client.post(
            CODEX_RESPONSES_URL,
            headers=_headers(access_token, account_id),
            json=body,
            timeout=httpx.Timeout(120.0, connect=30.0, read=120.0),
        )
    except httpx.HTTPError as e:
        print(f"  NETWORK ERROR: {type(e).__name__}: {e}")
        return {"ok": False, "network_error": str(e)}

    ct = r.headers.get("content-type", "")
    server = r.headers.get("server", "")
    cf_ray = r.headers.get("cf-ray", "")
    print(
        f"  -> HTTP {r.status_code}  content-type={ct!r}  server={server!r}"
        f"  cf-ray={cf_ray!r}"
    )
    # surface rate-limit headers if present
    rl = {
        k: v
        for k, v in r.headers.items()
        if "ratelimit" in k.lower() or "retry-after" in k.lower()
    }
    if rl:
        print(f"  rate-limit headers: {rl}")

    if "text/event-stream" in ct:
        summary = _summarize_sse(r.text)
        print(f"  SSE event_seq: {summary['event_seq']}")
        print(f"  SSE [DONE]: {summary['saw_[DONE]']}  terminal: {summary['terminal']}")
        if summary["text_preview"]:
            print(f"  text preview: {summary['text_preview']!r}")
        return {"ok": r.is_success, "status": r.status_code, "sse": summary}

    # non-SSE: JSON or HTML (HTML => Cloudflare/challenge)
    snippet = r.text[:400]
    is_html = "<html" in snippet.lower() or "cloudflare" in snippet.lower()
    print(f"  {'HTML/CHALLENGE' if is_html else 'body'}: {snippet!r}")
    return {
        "ok": r.is_success,
        "status": r.status_code,
        "html": is_html,
        "body": snippet,
    }


_BASELINE_KEYS = {
    "model",
    "instructions",
    "input",
    "store",
    "stream",
    "tools",
    "tool_choice",
}


def _probe_refresh_rotation(refresh_token: str) -> None:
    """Answer: does OpenAI rotate the refresh token on a refresh grant?
    Does NOT persist anything — we discard whatever we get back."""
    print("\n=== refresh-token rotation check ===")
    print(f"  current refresh_token: {_fp(refresh_token)}")
    try:
        with httpx.Client() as c:
            r = c.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
    except httpx.HTTPError as e:
        print(f"  NETWORK ERROR: {e}")
        return
    print(f"  -> HTTP {r.status_code}")
    if not r.is_success:
        print(f"  refresh FAILED body: {r.text[:300]!r}")
        return
    data = r.json()
    new_refresh = data.get("refresh_token", "")
    new_access = data.get("access_token", "")
    rotated = bool(new_refresh) and new_refresh != refresh_token
    print(f"  new access_token: {_fp(new_access)}")
    print(f"  new refresh_token: {_fp(new_refresh)}")
    print(
        f"  ROTATED refresh token: {rotated}  "
        f"(returned_refresh={'yes' if new_refresh else 'no'})"
    )
    print(
        "  NOTE: not persisted. The real bridge will write-back; a rotated "
        "token means the codex CLI's stored token is now stale unless we save it."
    )


def main() -> None:
    print("=" * 70)
    print("CODEX BACKEND PROBE — plain httpx, single account, this machine")
    print("=" * 70)
    auth = _load_auth()
    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    account_id = _account_id(auth, access_token)
    if not access_token:
        sys.exit("[probe] auth.json has no tokens.access_token")

    print(f"auth_mode: {auth.get('auth_mode')}")
    print(f"access_token: {_fp(access_token)}  ({_exp_status(access_token)})")
    print(
        f"account_id: {account_id!r}  (source: "
        f"{'auth.json' if tokens.get('account_id') else 'JWT claim'})"
    )

    results: dict[str, Any] = {}
    with httpx.Client(http2=False) as client:
        # 1. GATE: minimal happy path on the wanted model.
        results["happy"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL),
            label=f"happy path — {PRIMARY_MODEL} (GATE)",
        )
        # 2. plain-httpx / Cloudflare check is implicit in (1): HTML or 403 => blocked.
        # 3. non-streaming rejection.
        results["nonstream"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, stream=False),
            label="non-streaming (expect rejection or forced-stream)",
        )
        # 4. store:true rejection.
        results["store_true"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, store=True),
            label="store:true (expect rejection)",
        )
        # 5. strip-list params: temperature / max_output_tokens / top_p.
        results["temperature"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, extra={"temperature": 0.5}),
            label="temperature:0.5 (expect rejection -> strip)",
        )
        results["max_output_tokens"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, extra={"max_output_tokens": 64}),
            label="max_output_tokens:64 (expect rejection -> strip)",
        )
        results["top_p"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, extra={"top_p": 0.9}),
            label="top_p:0.9 (expect rejection -> strip)",
        )
        # 6. encrypted-reasoning include acceptance.
        results["include_reasoning"] = _post(
            client,
            access_token,
            account_id,
            _build_body(
                PRIMARY_MODEL,
                extra={
                    "include": ["reasoning.encrypted_content"],
                    "reasoning": {"effort": "low"},
                },
            ),
            label="include reasoning.encrypted_content (expect accept)",
        )
        # 7. long/complex instructions tolerance.
        results["long_instructions"] = _post(
            client,
            access_token,
            account_id,
            _build_body(PRIMARY_MODEL, instructions="X" * 6000),
            label="long instructions ~6k chars (expect reject -> fold into user)",
        )
        # 8. informational: other model ids.
        for m in OTHER_MODELS:
            results[f"model:{m}"] = _post(
                client,
                access_token,
                account_id,
                _build_body(m),
                label=f"model probe — {m} (informational)",
            )

    # 9. refresh rotation (separate endpoint).
    if refresh_token:
        _probe_refresh_rotation(refresh_token)
    else:
        print("\n[warn] no refresh_token in auth.json — skipping rotation check")

    # --- verdict ------------------------------------------------------------
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    happy = results.get("happy", {})
    if happy.get("html") or happy.get("status") in (403, 401) and happy.get("html"):
        print("BLOCKED: got an HTML/challenge or 403 on the happy path.")
        print("-> Do NOT build the bridge on plain httpx. Fall back to API key.")
        sys.exit(2)
    if happy.get("ok"):
        term = (happy.get("sse") or {}).get("terminal", {})
        print(f"PASS: {PRIMARY_MODEL} returned a successful SSE stream.")
        print(f"      terminal event = {term.get('type')} status={term.get('status')}")
        print("-> Proceed to Phase 1 (simple near-passthrough bridge).")
    else:
        print(
            f"INCONCLUSIVE: happy path HTTP {happy.get('status')} — "
            "read the transcript above (may be model-id or transient)."
        )
        print("-> Inspect before proceeding.")


if __name__ == "__main__":
    main()
