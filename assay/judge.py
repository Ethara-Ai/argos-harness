"""Run the judge council over a task's recorded runs.

Previously a scratch script driven by eight environment variables. A stale
preamble path in it silently dropped the six shared dimensions from every
verdict, which is the kind of defect that only surfaces when the code lives
outside the test suite.

Pacing matters and is not incidental. A trajectory dribbles one request every
few seconds and never trips the rolling rate window; a judge burst fires 40K
token calls back to back and does. The client keeps a floor between requests,
grows it on throttle and shrinks it on success, and honours the server's own
retry_after rather than guessing an exponential shorter than the real window.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Sequence


__all__ = [
    "COUNCIL",
    "JudgePlan",
    "already_done",
    "call",
    "judges_for_subject",
    "model_family",
    "plan_calls",
    "select_members",
    "shard",
    "verdict_path",
]

# Keyed by model, not by slot. A role label is not stable - `third` meant haiku
# until the council became two-vendor - and it is the aggregation key, so
# remapping a role would let verdicts from two different judges blend under one
# key. Keying on the model makes a swap orphan its verdicts, which forces a
# re-judge instead of quietly mixing eras, and makes the artifact name its judges.
#
# The key is the name the corpus records; the value is the name the bridge wants.
# Those diverged when milo-bench-samples was republished with the model as
# `opus-4.8`, and reading only the long form silently dropped every verdict that
# judge had produced - scoring_members filtered them out and the runs scored on
# the deterministic channel alone. A short model name is still a model name, so
# the invariant holds: what a verdict is keyed by identifies who produced it.
COUNCIL = {"opus-4.8": "claude-opus-4-8", "gpt-5.5": "gpt-5.5"}

ANCHOR_MODEL = "opus-4.8"

# aurora-harness deployment override: `ASSAY_COUNCIL="sonnet-5=claude-sonnet-5"`
# installs the single-judge roster decided for our pipeline (and disables the
# same-family exclusion below — a deliberate, user-approved deviation from the
# author≠judge design, since sonnet also authors the rubrics).
_ENV_COUNCIL = os.environ.get("ASSAY_COUNCIL", "").strip()
if _ENV_COUNCIL:
    COUNCIL = {
        alias.strip(): mid.strip()
        for alias, mid in (e.split("=", 1) for e in _ENV_COUNCIL.split(",") if "=" in e)
    }
    ANCHOR_MODEL = os.environ.get("ASSAY_ANCHOR", next(iter(COUNCIL)))

ALL_SEATS = COUNCIL
CROSS_FAMILY = {"gpt-5.5": "gpt-5.5"} if not _ENV_COUNCIL else {}

# claude-sonnet-5 authors the rubrics and therefore does not judge against them.
# An author scoring its own criteria grades the phrasing it chose rather than the
# run: where the wording is loose it knows what it meant, so it reads its own
# intent into the evidence. Keeping it out of ALL_SEATS makes that structural
# rather than a convention someone can forget.
RUBRIC_AUTHOR = "claude-sonnet-5"


def model_family(model: str) -> str:
    m = (model or "").lower()
    if any(k in m for k in ("claude", "opus", "sonnet", "haiku")):
        return "claude"
    if any(k in m for k in ("gpt", "openai", "codex", "o1", "o3", "o4")):
        return "gpt"
    return "other"


def judges_for_subject(subject_model: str) -> list[str]:
    """The judges that score a run, chosen by the family that produced it.

    A judge scores its own family generously - it recognises its house style as
    competence - so no run is scored by a relative. That leaves one judge for a
    run from a family we judge with, and for a run from neither family (gemini)
    there is no relative to exclude, so both score it. Those two-judge runs are
    also the only place inter-judge agreement can be read without a
    self-preference confound, which makes them the panel's calibration stratum.
    """
    if _ENV_COUNCIL:
        # explicit roster judges every subject (single-judge deployments)
        return sorted(COUNCIL)
    fam = model_family(subject_model)
    if fam == "other":
        return sorted(COUNCIL)
    return [m for m in sorted(COUNCIL) if model_family(m) != fam]


MAX_PACKET = 560_000
MAX_TOKENS = 4000
# Sonnet answers after a thinking block and occasionally spends the whole budget
# there, returning 200 with no text. Measured at 3 of 243 calls, so the larger
# budget is spent only on the retry rather than on every call.
MAX_TOKENS_RETRY = 12000
MIN_GAP, MAX_GAP = 3.0, 90.0
ITEM_DEADLINE = 1800.0

_PACE = {"gap": MIN_GAP, "last": 0.0}


def select_members(spec: str | None) -> list[str]:
    if not spec:
        return list(COUNCIL)
    names = [s.strip() for s in spec.split(",") if s.strip()]
    for n in names:
        if n not in ALL_SEATS:
            raise ValueError(
                f"unknown council member {n!r}; expected {sorted(ALL_SEATS)}"
            )
    return names


def shard(runs: Sequence[str], index: int, total: int) -> list[str]:
    return [r for i, r in enumerate(runs) if i % total == index]


from .bundle import _UUID_RE


def resolve_verdict_dir(out_dir: Path, uuid: str) -> Path:
    """The per-task verdict directory, whether the store root or it was given.

    Taking ``--out`` verbatim let verdicts land flat at the store root, where
    ``score --verdicts .../<uuid>`` never looks: the judge exited 0, the files
    were real, and the runs read as unjudged with no warning. Appending the
    uuid is idempotent, so both call styles arrive at the same directory, and a
    directory named for a different task is an error rather than a silent mix.
    """
    out_dir = Path(out_dir)
    if _UUID_RE.fullmatch(out_dir.name):
        if out_dir.name != uuid:
            raise ValueError(
                f"--out points at {out_dir.name}, which is a different task than {uuid}"
            )
        return out_dir
    return out_dir / uuid


def find_verdict_dir(given: Path, uuid: str) -> Path:
    """Where a task's verdicts actually are, reading rather than appending.

    The write side can append the uuid unconditionally; reading cannot, because
    a directory holding one task's files directly is a valid argument and
    appending would point at nothing. Preferring a populated per-task subdir
    accepts both shapes without a flag.
    """
    given = Path(given)
    scoped = given / uuid
    if scoped.is_dir() and any(scoped.glob("*.jsonl")):
        return scoped
    return given


def verdict_path(out_dir: Path, label: str) -> Path:
    model, run_id = label.split("/", 1)
    return Path(out_dir) / f"{model}__{run_id}.jsonl"


def already_done(path: Path) -> set[tuple[str, str]]:
    """(item, member) pairs whose stored completion actually parses.

    Keyed on parseability rather than presence: a blank left by a failed call
    must be retried, not treated as finished.
    """
    from .rubric import parse_verdict

    p = Path(path)
    if not p.is_file():
        return set()
    done = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if parse_verdict(rec.get("completion", ""), rec["item_id"], rec["member"]):
            done.add((rec["item_id"], rec["member"]))
    return done


def plan_calls(
    *, items: Iterable[str], members: Iterable[str], done: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Member-outer, so one model answers consecutively and the packet stays cached."""
    return [(i, m) for m in members for i in items if (i, m) not in done]


class JudgePlan:
    def __init__(
        self,
        *,
        uuid: str,
        runs: Sequence[str],
        items: Sequence[str],
        members: Sequence[str] | None = None,
        members_for: dict[str, Sequence[str]] | None = None,
    ):
        self.uuid, self.runs, self.items = uuid, runs, items
        self.members_for = members_for or {r: list(members or ()) for r in runs}

    @property
    def total_calls(self) -> int:
        return sum(len(self.items) * len(self.members_for[r]) for r in self.runs)


def gap_for_workers(workers: int) -> float:
    """The per-call gap that holds the process's call rate at 1/MIN_GAP.

    The gap is process-global, so without dividing it the worker threads queue
    behind one another and concurrency buys nothing: 48 in-flight ran at the
    same rate as 12.
    """
    return MIN_GAP / max(1, int(workers or 1))


def set_pace_for_workers(workers: int) -> None:
    _PACE["gap"] = gap_for_workers(workers)


def _pace() -> None:
    wait = _PACE["gap"] - (time.time() - _PACE["last"])
    if wait > 0:
        time.sleep(wait)
    _PACE["last"] = time.time()


# Beyond this a wait is not a backoff, it is a wall: the account's cap came back
# with 363812 seconds (101 hours) and the client paced against it for an hour.
CAP_RETRY_THRESHOLD = 900


def is_subscription_cap(doc, headers) -> bool:
    """True when the account is capped rather than momentarily throttled."""
    info = (doc or {}).get("aurora_bridge") or {}
    if info.get("kind") == "subscription_cap":
        return True
    ra = _retry_after(doc, headers)
    return bool(ra and ra >= CAP_RETRY_THRESHOLD)


def _retry_after(doc, headers=None) -> int | None:
    ra = (doc or {}).get("aurora_bridge", {}).get("retry_after_seconds")
    if ra is None and headers is not None:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        ra = int(raw) if raw and str(raw).isdigit() else None
    return int(ra) if ra else None


def is_thinking_exhaustion(error: str) -> bool:
    return "empty completion" in (error or "") and "max_tokens" in error


def is_openai_endpoint(proxy: str) -> bool:
    return "/chat/completions" in (proxy or "")


def is_responses_endpoint(proxy: str) -> bool:
    """The OpenAI Responses backend (via proxy/codex_bridge on :8766).

    Matches both ``/responses`` and ``/v1/responses`` by path suffix, and never
    the Anthropic bridge (``/v1/messages``) or the chat endpoint."""
    from urllib.parse import urlparse

    path = urlparse(proxy or "").path.rstrip("/")
    return path.endswith("/responses") or path == "/responses"


def build_request(
    proxy: str,
    model: str,
    system: str,
    question: str,
    cached: str = "",
    max_tokens: int = MAX_TOKENS,
) -> tuple[dict, dict]:
    """The body and headers for whichever API shape the endpoint speaks."""
    if is_responses_endpoint(proxy):
        # OpenAI Responses shape, spoken to proxy/codex_bridge (:8766). The bridge
        # strips Authorization + injects the real ChatGPT token, folds these
        # instructions into the user message, forces stream:true/store:false and
        # strips max_output_tokens, then aggregates the SSE back to one JSON doc.
        text = (
            f"{cached[:MAX_PACKET]}\n\n{question}" if cached else question[:MAX_PACKET]
        )
        body = {
            "model": model,
            "instructions": system,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            ],
            "store": False,
            "stream": False,
            "max_output_tokens": max_tokens,
        }
        stub = os.environ.get("ASSAY_CODEX_STUB_KEY", "sk-codex-oauth-bridge-stub")
        return body, {
            "content-type": "application/json",
            "Authorization": f"Bearer {stub}",
        }
    if is_openai_endpoint(proxy):
        user = (
            f"{cached[:MAX_PACKET]}\n\n{question}" if cached else question[:MAX_PACKET]
        )
        body = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        secret = os.environ.get("ASSAY_CODEX_SECRET", "assay-codex-seat")
        return body, {
            "content-type": "application/json",
            "Authorization": f"Bearer {secret}",
        }
    if cached:
        blocks = [
            {
                "type": "text",
                "text": cached[:MAX_PACKET],
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": question},
        ]
    else:
        blocks = question[:MAX_PACKET]
    # Two bridge requirements, both verified empirically against
    # proxy/claude_code_bridge (identical bodies flip 429→200):
    #  * `system` must be block-list form — the bridge prepends its Claude Code
    #    system block, and a plain-string system breaks that injection upstream;
    #  * an x-api-key header must be present (value is stripped and replaced
    #    with the real OAuth token).
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": blocks}],
    }
    return body, {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": os.environ.get(
            "ASSAY_BRIDGE_STUB_KEY", "sk-ant-oauth-bridge-stub"
        ),
    }


def extract_text(doc: dict) -> str:
    if "choices" in doc:
        return "".join(
            (c.get("message") or {}).get("content") or "" for c in doc["choices"]
        )
    # OpenAI Responses shape: walk output[] message items -> output_text parts.
    if "output" in doc:
        parts = []
        for item in doc.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
        return "".join(parts)
    return "\n".join(c.get("text", "") for c in (doc.get("content") or []))


def call(
    proxy: str,
    model: str,
    system: str,
    question: str,
    cached: str = "",
    max_tokens: int = MAX_TOKENS,
) -> tuple[str, str, int | None, int]:
    """Return (text, error, retry_after, status). status 429 means throttled."""
    body, headers = build_request(proxy, model, system, question, cached, max_tokens)
    req = urllib.request.Request(
        proxy, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            doc = json.loads(r.read())
        if doc.get("type") == "error":
            msg = doc["error"].get("message", "")
            status = 429 if "rate" in msg.lower() else 400
            return "", msg[:160], _retry_after(doc), status
        text = extract_text(doc)
        if not text.strip():
            # Sonnet emits a thinking block first and can spend the whole budget
            # there, returning 200 with no text. Reported as an error so resume
            # retries instead of storing a blank as a finished verdict.
            return (
                "",
                f"empty completion (stop_reason={doc.get('stop_reason')})",
                None,
                200,
            )
        return text, "", None, 200
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = {}
        return "", f"HTTP {e.code} {raw[:120]!r}", _retry_after(doc, e.headers), e.code
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}", None, 0


class SubscriptionCapped(RuntimeError):
    """The account's budget is exhausted; pacing cannot recover it."""


class BridgeUnreachable(RuntimeError):
    """No endpoint is listening; every later call fails the same way."""


# A refused connection or unresolvable host is a property of the endpoint, not
# of the request, so retrying per call cannot succeed. Recording it as an
# ordinary failure once wrote 2,754 records that each read as a judged item
# with no verdict.
_UNREACHABLE = (
    "connection refused",
    "nodename nor servname",
    "name or service not known",
    "no route to host",
    "connection reset by peer",
    "cannot assign requested address",
)


def is_unreachable(err: str) -> bool:
    low = (err or "").lower()
    return low.startswith("urlerror") and any(s in low for s in _UNREACHABLE)


def _error_doc(err: str) -> dict:
    m = re.search(r"\{.*\}", err or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def call_with_backoff(
    proxy: str, model: str, system: str, question: str, cached: str, deadline: float
) -> tuple[str, str]:
    budget, retried_for_thinking = MAX_TOKENS, False
    while True:
        _pace()
        raw, err, retry_after, status = call(
            proxy, model, system, question, cached, budget
        )
        if is_thinking_exhaustion(err) and not retried_for_thinking:
            retried_for_thinking, budget = True, MAX_TOKENS_RETRY
            continue
        if is_unreachable(err):
            raise BridgeUnreachable(
                f"nothing is listening at {proxy}: {err[:120]}. Start the "
                f"bridge or point ASSAY_PROXY at the live one."
            )
        if status in (401, 403):
            raise PermissionError(f"auth {status} from the bridge: {err[:120]}")
        if status == 429 and is_subscription_cap(_error_doc(err), None):
            raise SubscriptionCapped(
                f"the account is capped, not throttled; retry_after="
                f"{retry_after or 'unknown'}s. Waiting is not a backoff at that "
                f"scale - stop and resume when the window resets."
            )
        if raw:
            _PACE["gap"] = max(MIN_GAP, _PACE["gap"] * 0.9)
            return raw, ""
        if status != 429 and "rate" not in err.lower():
            return "", err
        _PACE["gap"] = min(MAX_GAP, _PACE["gap"] * 1.7)
        wait = max(retry_after or 0, _PACE["gap"]) + random.uniform(0, 5)
        if time.time() + wait > deadline:
            return "", err or "deadline exceeded under throttle"
        time.sleep(wait)


def pending_calls(*, items, members, done) -> int:
    """How many calls a run still needs, without building anything.

    Consulted before the packet is reconstructed: a finished run otherwise pays
    the full edit reconstruction to issue no calls at all.
    """
    return len(list(plan_calls(items=items, members=members, done=done)))
