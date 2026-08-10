"""Codex (ChatGPT-subscription) OAuth bridge for the aurora harness.

A local OpenAI-Responses proxy (default port 8766) that injects the Codex
subscription token from ``~/.codex/auth.json`` so trajectory generation (and,
optionally, rubric authoring/judging) runs on the ChatGPT subscription instead
of an ``OPENAI_API_KEY``. Sibling of ``claude_code_bridge`` (port 8765).

Run via ``python -m codex_bridge`` or ``proxy/codex_bridge.sh start``.
"""

from __future__ import annotations

from .bridge import build_app
from .credentials import CodexCredentialProvider, CredentialsError
from .shaping import shape_body


__all__ = [
    "build_app",
    "CodexCredentialProvider",
    "CredentialsError",
    "shape_body",
]
