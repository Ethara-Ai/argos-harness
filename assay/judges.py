"""Judge transports.

Three ways to obtain a verdict, all satisfying the same ``JudgeFn`` contract of
``(system, user) -> raw text``:

``bedrock``  AWS Bedrock converse, the shape WildClawBench uses in production.
``openai``   OpenAI chat completions.
``replay``   read verdicts from a JSONL file on disk.

``replay`` is not a mock. It is how a graded run is made reproducible: a council
pass is expensive and non-deterministic, so the raw completions are recorded once
and replayed for every later re-score. It is also the path that lets a human sit
in as a council member, which matters when a rubric is new and its agreement
numbers are not yet trusted.

Members are keyed by a stable family label rather than by model id, because model
ids and ARNs rotate while the roster's meaning does not. Evidence budget and
context limits key off the family.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_TIMEOUT = 180
DEFAULT_MAX_TOKENS = 700


@dataclass(frozen=True)
class Member:
    family: str
    model_id: str
    transport: str
    max_evidence_chars: int = 400_000
    max_tokens: int = DEFAULT_MAX_TOKENS


def bedrock_judge(
    member: Member, *, region: str = "us-east-1", timeout: int = DEFAULT_TIMEOUT
) -> Callable[[str, str], str]:
    token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")

    def call(system: str, user: str) -> str:
        if not token:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK is not set")
        url = (
            f"https://bedrock-runtime.{region}.amazonaws.com/model/"
            f"{member.model_id}/converse"
        )
        body = {
            "system": [{"text": system}],
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": user[: member.max_evidence_chars]}],
                }
            ],
            "inferenceConfig": {"maxTokens": member.max_tokens, "temperature": 0},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - internally-constructed bridge URL, not user input
            doc = json.loads(resp.read())
        return _bedrock_text(doc)

    return call


def _bedrock_text(doc: dict[str, Any]) -> str:
    content = ((doc.get("output") or {}).get("message") or {}).get("content") or []
    return "\n".join(c.get("text", "") for c in content)


def openai_judge(
    member: Member, *, base_url: str = "", timeout: int = DEFAULT_TIMEOUT
) -> Callable[[str, str], str]:
    key = os.environ.get("OPENAI_API_KEY", "")
    base = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def call(system: str, user: str) -> str:
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        body = {
            "model": member.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[: member.max_evidence_chars]},
            ],
            "max_completion_tokens": member.max_tokens,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - internally-constructed bridge URL, not user input
            doc = json.loads(resp.read())
        return (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")

    return call


class ReplayJudge:
    """Serve recorded completions keyed by (member, item id).

    Raises on a miss rather than returning something plausible. A replay that
    quietly invents a verdict for an item it has never seen would produce a
    complete-looking report built partly on nothing.
    """

    def __init__(self, path: str | Path, member: str):
        self.member = member
        self.by_item: dict[str, str] = {}
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"no verdict log at {p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("member") != member:
                continue
            self.by_item[str(rec["item_id"])] = str(rec["completion"])

    def for_item(self, item_id: str) -> Callable[[str, str], str]:
        def call(_system: str, _user: str) -> str:
            if item_id not in self.by_item:
                raise KeyError(f"no recorded verdict for {self.member}/{item_id}")
            return self.by_item[item_id]

        return call


def record_verdict(
    path: str | Path, member: str, item_id: str, completion: str, **extra: Any
) -> None:
    rec = {"member": member, "item_id": item_id, "completion": completion, **extra}
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
