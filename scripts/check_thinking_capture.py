"""Quick host-side probe: is thinking capture working on this machine?

Answers the "sometimes I see thinking tokens, sometimes not" question in ~30
seconds instead of an hour-long eval run. Sends a few small completions
through the exact same path an eval uses (LLM config -> bridge -> litellm ->
SDK message types) and reports what came back.

Interpretation (mirrors THINKING_CAPTURE_JOURNAL.md / SCORE_MATH.md §10):
- no thinking_blocks at all      -> thinking never requested (config/model
                                    routing) or bridge unreachable: OUR problem.
- blocks but empty text/0 tokens -> signature-only thinking: upstream Anthropic
                                    behavior, NOT a harness bug. Reasoning
                                    tokens are litellm's re-tokenization of the
                                    returned text, so these turns count 0.
- blocks + text + tokens         -> capture fully working.

Usage:
    uv run python scripts/check_thinking_capture.py [.llm_config/claude-code.json]
    uv run python scripts/check_thinking_capture.py --attempts 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from openhands.sdk.llm import LLM, Message, TextContent
from openhands.sdk.llm.utils.model_features import get_features


PROMPT = (
    "A train leaves at 14:35 and arrives at 17:12 the same day. "
    "How many minutes is the journey? Reason it through carefully."
)

# The probe runs on the host, so container-facing bridge hosts must be
# rewritten to loopback (the bridge binds 0.0.0.0, loopback always works).
_CONTAINER_HOSTS = re.compile(
    r"^(http://)(172\.17\.[0-9]+\.[0-9]+|host\.docker\.internal)"
)


def _load_llm(config_path: Path, base_url_override: str | None) -> LLM:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = cfg.get("base_url", "")
    if base_url_override:
        cfg["base_url"] = base_url_override
    elif isinstance(base_url, str):
        cfg["base_url"] = _CONTAINER_HOSTS.sub(r"\g<1>127.0.0.1", base_url)
    if cfg["base_url"] != base_url:
        print(f"probe: base_url {base_url} -> {cfg['base_url']} (host-side)")
    return LLM.model_validate(cfg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "config",
        nargs="?",
        default=".llm_config/claude-code.json",
        help="LLM config JSON (default: .llm_config/claude-code.json)",
    )
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--base-url", default=None, help="override base_url entirely")
    args = ap.parse_args()

    llm = _load_llm(Path(args.config), args.base_url)
    features = get_features(llm.model)
    thinking_requested = bool(llm.force_adaptive_thinking) and (
        features.supports_adaptive_thinking
    )
    print(f"model: {llm.model}")
    print(
        f"thinking requested: {thinking_requested} "
        f"(force_adaptive_thinking={llm.force_adaptive_thinking}, "
        f"supports_adaptive_thinking={features.supports_adaptive_thinking})"
    )
    if not thinking_requested:
        print(
            "FAIL: this config/model pair never asks for thinking - "
            "fix the config (force_adaptive_thinking) or the model id "
            "before blaming capture."
        )
        return 1

    rows: list[tuple[int, int, int, int, int]] = []
    for i in range(1, args.attempts + 1):
        resp = llm.completion(
            messages=[Message(role="user", content=[TextContent(text=PROMPT)])]
        )
        msg = resp.message
        blocks = list(msg.thinking_blocks)
        with_text = sum(1 for b in blocks if bool(getattr(b, "thinking", "") or ""))
        reasoning_chars = len(msg.reasoning_content or "")
        usage = llm.metrics.token_usages[-1] if llm.metrics.token_usages else None
        r_tokens = usage.reasoning_tokens if usage else 0
        rows.append((i, len(blocks), with_text, reasoning_chars, r_tokens))
        print(
            f"attempt {i}: thinking_blocks={len(blocks)} "
            f"(with text: {with_text}), reasoning_content={reasoning_chars} chars, "
            f"reasoning_tokens={r_tokens}"
        )

    n_usages = len(llm.metrics.token_usages)
    ids_ok = all(u.response_id for u in llm.metrics.token_usages)
    print(f"token_usages recorded: {n_usages} (all with response_id: {ids_ok})")

    any_blocks = any(r[1] > 0 for r in rows)
    any_text = any(r[2] > 0 for r in rows)
    any_tokens = any(r[4] > 0 for r in rows)

    if not any_blocks:
        print(
            "FAIL: thinking was requested but no attempt returned a thinking "
            "block. Suspect the bridge/base_url or litellm dropping the "
            "thinking param - this IS a harness-side problem."
        )
        return 1
    if n_usages == 0 or not ids_ok:
        print("FAIL: metrics plumbing broken (token_usages missing/unattributed).")
        return 1
    if not any_text:
        print(
            "PASS (with caveat): capture plumbing works - blocks arrived and "
            "were recorded - but every block was signature-only this time, so "
            "reasoning text/tokens are 0. That is upstream Anthropic "
            "behavior; re-run or raise --attempts to see text turns."
        )
        return 0
    print(
        f"PASS: thinking blocks, reasoning text and reasoning tokens all "
        f"captured (text on {sum(1 for r in rows if r[2] > 0)}/{len(rows)} "
        f"attempts, tokens {'>0' if any_tokens else '=0'})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
