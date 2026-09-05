"""The code-quality channel: a fixed seven-item block judged on the final diff.

Why a separate channel and not seven more rubric items:

* ``assay/lint.py`` rejects any dimension outside its six, and
  ``rubric.dimension_weights`` gives an unknown dimension a zero budget, so
  quality items dropped into ``rubrics.json`` would be judged, paid for, and
  worth nothing.
* ``fingerprint.bundle_fingerprint`` digests every item in ``rubrics.json``;
  adding items there would invalidate every recorded correctness verdict.
* The requirement is that quality be decoupled from outcome. It is published
  beside the reward fields and never enters ``process``, ``score_eval`` or
  ``score_rl``; ``assay/compose.py`` is untouched by this module.

The bundle's own ``tests/quality.json`` is the authority for what was judged:
every loader here takes that path, never the package manifest, so a bundle
cannot claim one block while being graded with another and a replay on a
checkout with a changed manifest cannot silently change its meaning.

What the judge sees is the task instruction and the agent's shipped patch,
nothing else. The reference TRUTH is withheld on purpose: the question is
whether the change is well made, not whether it resembles the oracle.

Everything a verdict depends on is pinned in ``quality_fingerprint``: the
scoring version, the prompt digest (system prompt plus the bundle's items),
the judge model, and a digest of the exact evidence text the judge received
(clipped instruction plus the patch as shown, so clipping is bound too).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .rubric import ItemOutcome, RubricItem, Verdict, aggregate, parse_verdict


QUALITY_VERSION = "quality-v1"
MANIFEST_PATH = Path(__file__).with_name("quality.json")
FINGERPRINT_KEY = "quality_fingerprint"
VERDICT_SUFFIX = "__quality"
GOLD_LABEL = "gold"

QUALITY_DIMENSIONS = frozenset(
    {
        "conventions",
        "minimal_diff",
        "abstraction",
        "readability",
        "naming",
        "idiomaticity",
        "dead_code",
    }
)

INSTRUCTION_CHARS = 4000
# UTF-8 bytes. Same budget the anchoring gate uses for the gold patch: at the
# packet builder's 60k default a 1 MB patch was 5% visible.
PATCH_CAP_BYTES = 1_500_000

# The calibration protocol's floors. ``quality-calibrate`` can be run with
# stricter flags; ``calibration_check`` refuses a calibration document whose
# recorded gates are looser than these, so a hand-edited file cannot license
# publication.
DEFAULT_GATES: dict[str, float] = {
    "min_kappa": 0.60,
    "min_spearman": 0.60,
    "min_pearson": 0.60,
    "min_balanced_accuracy": 0.65,
    "min_subjects": 50,
    "min_dev": 20,
    "min_holdout": 30,
    "min_raters": 2,
}

QUALITY_SYSTEM = """You are reviewing the craft of a code change, the way a careful maintainer \
reviews a pull request. You are not deciding whether the change fixes the task; a \
separate verifier already decided that and you have not been told its result.

You will be given the task the author was set, the full diff they produced, and one \
criterion. Answer only that criterion, about this diff.

Rules:
- Judge only what the diff shows. Context lines show the surrounding code's habits; \
added and removed lines show the change. If the diff does not show it, the answer is No.
- Answer the criterion exactly as written. Do not reason about whether it is good or bad \
that the criterion holds.
- A change can be correct and still badly made, or wrong and still well made. Do not let \
either judgement leak into the other.
- Identical diffs must always produce the same answer. There is no partial credit.
- Cite the file and hunk you relied on.
- If the diff was cut off before the part you needed, say so in TRUNCATION_AFFECTED \
rather than guessing.

Reply in exactly this format and nothing else:

[[RATIONALE: one or two sentences pointing at what in the diff decided it]]
[[SATISFIED: Yes or No]]
[[TRUNCATION_AFFECTED: Yes or No]]
[[EVIDENCE: file and hunk, or a short quote]]"""


# -- manifest --------------------------------------------------------------


class ManifestMissing(FileNotFoundError):
    """The bundle carries no tests/quality.json; nothing may be judged or scored."""


def load_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ManifestMissing(
            f"{p} is missing; run `assay quality-init` to materialize the block"
        )
    doc = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"quality manifest {p} must be a JSON object")
    return doc


def _canonical_items(doc: dict[str, Any]) -> str:
    return json.dumps(doc.get("items") or [], sort_keys=True, separators=(",", ":"))


def manifest_digest(path: str | Path) -> str:
    return hashlib.sha256(_canonical_items(load_manifest(path)).encode()).hexdigest()[
        :16
    ]


def prompt_digest(path: str | Path) -> str:
    """Pins the judge's instructions: system prompt plus the items it is asked."""
    blob = QUALITY_SYSTEM + "\x00" + _canonical_items(load_manifest(path))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_quality_items(path: str | Path) -> list[RubricItem]:
    """The seven items of one bundle, weighted as authored (no budget applies).

    Validated on load rather than trusted: a manifest with a missing dimension
    would make every run score against six items and read as complete.
    """
    doc = load_manifest(path)
    if str(doc.get("quality_version")) != QUALITY_VERSION:
        raise ValueError(
            f"quality manifest version {doc.get('quality_version')!r} is not "
            f"{QUALITY_VERSION!r}"
        )
    items: list[RubricItem] = []
    for raw in doc.get("items") or []:
        w = int(raw.get("weight") or 0)
        if w <= 0:
            raise ValueError(f"quality item {raw.get('id')} must have positive weight")
        items.append(
            RubricItem(
                id=str(raw.get("id")),
                dimension=str(raw.get("dimension") or ""),
                weight=w,
                evaluation_target=str(raw.get("evaluation_target") or "final_diff"),
                criterion=" ".join(str(raw.get("criterion") or "").split()),
                judgment=" ".join(str(raw.get("evaluation_rule") or "").split()),
                evidence=tuple(raw.get("evidence") or ("final_diff",)),
                effective_weight=float(w),
            )
        )
    dims = [i.dimension for i in items]
    if sorted(dims) != sorted(QUALITY_DIMENSIONS):
        raise ValueError(
            f"quality manifest dimensions {sorted(dims)} must be exactly "
            f"{sorted(QUALITY_DIMENSIONS)}"
        )
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("quality manifest has duplicate item ids")
    return items


# -- judge seat -------------------------------------------------------------


@dataclass(frozen=True)
class JudgeSeat:
    alias: str
    """Verdict record ``member``; what the shell derives from judge_model."""
    model: str
    """Bare id sent on the wire."""
    model_id: str
    """The configured litellm id, kept for pinning."""
    endpoint: str


def resolve_single_judge(
    cfg_path: str | Path, proxy_override: str | None = None
) -> JudgeSeat:
    """One seat from ``judge_model``, derived exactly as run_eval.sh does.

    ``openai/[responses/]x`` rides the Codex Responses bridge, anything else
    the Anthropic bridge. The alias drops a leading ``claude-`` so it matches
    the rubric channel's member names. This never consults ``ASSAY_COUNCIL``
    or the corpus roster in ``assay.judge``: a standalone quality run must
    grade with the configured judge or refuse, not fall back to two seats.
    """
    raw = json.loads(Path(cfg_path).read_text(encoding="utf-8-sig"))
    model_id = str(raw.get("judge_model") or raw.get("model") or "").strip()
    if not model_id:
        raise ValueError(f"{cfg_path}: missing judge_model")
    if model_id.startswith("openai/"):
        bare = model_id[len("openai/") :]
        bare = bare[len("responses/") :] if bare.startswith("responses/") else bare
        endpoint = "http://127.0.0.1:8766/responses"
    else:
        bare = (
            model_id[len("anthropic/") :]
            if model_id.startswith("anthropic/")
            else model_id
        )
        endpoint = "http://127.0.0.1:8765/v1/messages"
    alias = bare[len("claude-") :] if bare.startswith("claude-") else bare
    if not alias or not bare:
        raise ValueError(f"{cfg_path}: judge_model {model_id!r} has no model name")
    return JudgeSeat(
        alias=alias, model=bare, model_id=model_id, endpoint=proxy_override or endpoint
    )


# -- evidence -----------------------------------------------------------------


def clip_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut at a UTF-8 byte budget without splitting a code point."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def build_quality_packet(
    instruction: str, patch: str, *, patch_cap: int = PATCH_CAP_BYTES
) -> tuple[str, bool]:
    """Task instruction plus the diff. Returns (text, patch_was_truncated)."""
    shown, truncated = clip_utf8(patch, patch_cap)
    parts = [
        "## Task given to the author\n",
        _clip(instruction, INSTRUCTION_CHARS),
        "\n\n## The change, as a unified diff against the base commit\n",
        "```diff\n",
        shown,
        "\n```\n",
    ]
    if truncated:
        parts.append(
            f"\nNote: the diff was cut at {patch_cap} bytes of "
            f"{len(patch.encode('utf-8'))}; anything after that is not shown.\n"
        )
    return "".join(parts), truncated


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n[... {len(text) - n} chars cut]"


def evidence_digest(packet: str) -> str:
    """Digest of the exact text the judge saw (instruction, patch, clipping)."""
    return hashlib.sha256(packet.encode("utf-8")).hexdigest()[:16]


def quality_fingerprint(*, judge_model: str, evidence: str, prompt: str) -> str:
    """What one verdict depends on. Any change here means re-judging that run."""
    blob = "\x00".join([QUALITY_VERSION, prompt, judge_model, evidence])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def verdict_file(out_dir: str | Path, label: str) -> Path:
    """``<model>__<run>__quality.jsonl``; the gold patch is ``gold__quality.jsonl``."""
    if label == GOLD_LABEL:
        return Path(out_dir) / f"{GOLD_LABEL}{VERDICT_SUFFIX}.jsonl"
    model, run_id = label.split("/", 1)
    return Path(out_dir) / f"{model}__{run_id}{VERDICT_SUFFIX}.jsonl"


# -- replay and report --------------------------------------------------------


def read_records(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def replay_outcomes(
    records: Iterable[dict[str, Any]],
    items: Sequence[RubricItem],
    member: str,
    method: str = "majority",
) -> list[ItemOutcome]:
    """Resolve the recorded completions of one member into per-item outcomes."""
    by_item: dict[str, list[Verdict]] = {}
    for rec in records:
        if rec.get("member") != member:
            continue
        v = parse_verdict(str(rec.get("completion") or ""), str(rec["item_id"]), member)
        if v is not None:
            by_item.setdefault(str(rec["item_id"]), []).append(v)
    return [aggregate(i, by_item.get(i.id, []), member, method) for i in items]


@dataclass(frozen=True)
class PatchInfo:
    source: str
    sha256: str
    bytes: int
    truncated: bool


@dataclass
class QualityReport:
    task_uuid: str
    model: str
    run_id: str
    status: str
    """scored | unjudged | evidence_missing | empty_patch"""
    judge: str | None
    judge_model: str | None
    patch: PatchInfo | None
    outcomes: list[ItemOutcome] = field(default_factory=list)
    calibrated: bool = False
    fingerprint: str | None = None
    prompt: str | None = None
    manifest: str | None = None
    evidence: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def decided(self) -> list[ItemOutcome]:
        return [o for o in self.outcomes if o.satisfied is not None]

    @property
    def undecided(self) -> list[ItemOutcome]:
        return [o for o in self.outcomes if o.satisfied is None]

    @property
    def score(self) -> float | None:
        """Weighted pass ratio over all seven, or None. Never renormalised:
        six answers are not a score on a seven-item scale."""
        if self.status != "scored" or not self.outcomes or self.undecided:
            return None
        total = sum(o.item.scoring_weight for o in self.outcomes)
        if total <= 0:
            return None
        got = sum(o.item.scoring_weight for o in self.outcomes if o.satisfied)
        return round(got / total, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": {
                "quality": QUALITY_VERSION,
                "prompt_digest": self.prompt,
                "manifest_digest": self.manifest,
            },
            "task_uuid": self.task_uuid,
            "model": self.model,
            "run_id": self.run_id,
            "status": self.status,
            "calibrated": self.calibrated,
            "judge": {"member": self.judge, "model": self.judge_model},
            "patch": (
                {
                    "source": self.patch.source,
                    "sha256": self.patch.sha256,
                    "bytes": self.patch.bytes,
                    "truncated": self.patch.truncated,
                }
                if self.patch
                else None
            ),
            "evidence_digest": self.evidence,
            "quality_fingerprint": self.fingerprint,
            "score": self.score,
            "counts": {
                "total": len(self.outcomes),
                "passed": sum(1 for o in self.decided if o.satisfied),
                "failed": sum(1 for o in self.decided if not o.satisfied),
                "undecided": len(self.undecided),
            },
            "reasons": list(self.reasons),
            "items": [
                {
                    "id": o.item.id,
                    "dimension": o.item.dimension,
                    "weight": o.item.weight,
                    "resolution": o.resolution.value,
                    "satisfied": o.satisfied,
                    "rationale": (o.used[0].rationale[:400] if o.used else ""),
                    "evidence_ref": (o.used[0].evidence_ref if o.used else ""),
                }
                for o in self.outcomes
            ],
        }

    def render_md(self) -> str:
        from .quality_cli import quality_md_from_doc

        return quality_md_from_doc(self.to_dict())


QUALITY_MD_START = "<!-- quality -->"
QUALITY_MD_END = "<!-- /quality -->"


def upsert_quality_block(md: str, block: str) -> str:
    """Replace or append the quality block in a final_score.md text."""
    start = md.find(QUALITY_MD_START)
    end = md.find(QUALITY_MD_END)
    if start != -1 and end != -1 and end > start:
        end += len(QUALITY_MD_END)
        rest = md[end:].lstrip("\n")
        return md[:start].rstrip("\n") + "\n\n" + block + ("\n" + rest if rest else "")
    return md.rstrip("\n") + "\n\n" + block


def build_report(
    *,
    task_uuid: str,
    model: str,
    run_id: str,
    manifest_path: str | Path,
    instruction: str,
    patch: str | None,
    patch_sha256: str | None,
    records: Sequence[dict[str, Any]],
    seat: JudgeSeat | None,
    calibrated: bool,
    method: str = "majority",
    patch_cap: int = PATCH_CAP_BYTES,
) -> QualityReport:
    """Everything that decides a run's quality status, in one place."""
    items = load_quality_items(manifest_path)
    prompt = prompt_digest(manifest_path)
    rep = QualityReport(
        task_uuid=task_uuid,
        model=model,
        run_id=run_id,
        status="unjudged",
        judge=seat.alias if seat else None,
        judge_model=seat.model_id if seat else None,
        patch=None,
        calibrated=calibrated,
        prompt=prompt,
        manifest=manifest_digest(manifest_path),
    )
    if patch is None:
        rep.status = "evidence_missing"
        rep.reasons.append("no artifacts/agent.patch shipped with this run")
        return rep
    if not patch.strip():
        rep.status = "empty_patch"
        rep.reasons.append("the recorded patch is empty; nothing to review")
        return rep
    sha = patch_sha256 or hashlib.sha256(patch.encode("utf-8")).hexdigest()
    packet, truncated = build_quality_packet(instruction, patch, patch_cap=patch_cap)
    rep.patch = PatchInfo("git", sha, len(patch.encode("utf-8")), truncated)
    rep.evidence = evidence_digest(packet)
    if seat is None:
        rep.reasons.append("no judge configured")
        return rep
    rep.fingerprint = quality_fingerprint(
        judge_model=seat.model_id, evidence=rep.evidence, prompt=prompt
    )
    if not records:
        rep.reasons.append("no quality verdicts recorded for this run")
        return rep
    strangers = sorted({str(r.get("member")) for r in records} - {seat.alias})
    if strangers:
        rep.reasons.append(
            f"verdicts from {strangers} but the configured judge is {seat.alias!r}"
        )
        return rep
    stale = sorted({str(r.get(FINGERPRINT_KEY)) for r in records} - {rep.fingerprint})
    if stale:
        rep.reasons.append(
            f"verdicts carry fingerprint(s) {stale}, current is {rep.fingerprint}; "
            "the prompt, judge, instruction or patch changed since judging"
        )
        return rep
    rep.outcomes = replay_outcomes(records, items, seat.alias, method)
    if rep.undecided:
        rep.reasons.append(
            "undecided: "
            + ", ".join(f"{o.item.id}={o.resolution.value}" for o in rep.undecided)
        )
        return rep
    rep.status = "scored"
    return rep


# -- calibration --------------------------------------------------------------


CALIBRATION_REQUIRED = (
    "quality_version",
    "model",
    "prompt_digest",
    "manifest_digest",
    "passed",
    "n_subjects",
    "n_dev",
    "n_holdout",
    "n_raters",
    "inter_rater_weighted_kappa",
    "holdout_spearman",
    "holdout_pearson",
    "per_dimension",
    "gates",
)


def _meets(value: Any, floor: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= floor
    )


def calibration_passes(
    doc: dict[str, Any], gates: dict[str, float]
) -> tuple[bool, str]:
    """Recompute the verdict from the recorded metrics against the given gates.

    Used both when writing the document and when reading it back, so a file
    whose ``passed`` disagrees with its own numbers cannot license anything.
    """
    for k in ("n_subjects", "n_dev", "n_holdout", "n_raters"):
        if not _meets(doc.get(k), gates[f"min_{k[2:]}"]):
            return False, f"{k}={doc.get(k)} below {gates[f'min_{k[2:]}']}"
    if not _meets(doc.get("inter_rater_weighted_kappa"), gates["min_kappa"]):
        return (
            False,
            f"kappa={doc.get('inter_rater_weighted_kappa')} below {gates['min_kappa']}",
        )
    if not _meets(doc.get("holdout_spearman"), gates["min_spearman"]):
        return (
            False,
            f"spearman={doc.get('holdout_spearman')} below {gates['min_spearman']}",
        )
    if not _meets(doc.get("holdout_pearson"), gates["min_pearson"]):
        return (
            False,
            f"pearson={doc.get('holdout_pearson')} below {gates['min_pearson']}",
        )
    per_dim = doc.get("per_dimension") or {}
    if sorted(per_dim) != sorted(QUALITY_DIMENSIONS):
        return False, "per_dimension does not cover the seven dimensions"
    for d, v in per_dim.items():
        if v.get("both_classes") and not _meets(
            v.get("balanced_accuracy"), gates["min_balanced_accuracy"]
        ):
            return (
                False,
                f"{d} balanced accuracy {v.get('balanced_accuracy')} below {gates['min_balanced_accuracy']}",
            )
    if doc.get("stale_subjects"):
        return (
            False,
            f"{len(doc['stale_subjects'])} subject(s) judged under a stale fingerprint",
        )
    return True, "calibrated"


def calibration_check(
    calibration_path: str | Path, *, judge_model: str, prompt: str, manifest: str
) -> tuple[bool, str]:
    """Whether a bundle's judge_calibration.json licenses publishing score_quality.

    The document is not trusted: its recorded gates must be at least the
    protocol floors and its own metrics must clear them.
    """
    p = Path(calibration_path)
    if not p.is_file():
        return False, f"no {p.name} in the bundle"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, f"{p.name} is not valid JSON: {exc}"
    missing = [k for k in CALIBRATION_REQUIRED if k not in doc]
    if missing:
        return False, f"{p.name} lacks {missing}"
    if str(doc.get("quality_version")) != QUALITY_VERSION:
        return (
            False,
            f"{p.name} is for {doc.get('quality_version')!r}, not {QUALITY_VERSION!r}",
        )
    if str(doc.get("model")) != judge_model:
        return (
            False,
            f"{p.name} calibrated {doc.get('model')!r}, judging with {judge_model!r}",
        )
    if str(doc.get("prompt_digest")) != prompt:
        return (
            False,
            f"{p.name} calibrated prompt {doc.get('prompt_digest')}, current {prompt}",
        )
    if str(doc.get("manifest_digest")) != manifest:
        return (
            False,
            f"{p.name} calibrated manifest {doc.get('manifest_digest')}, this bundle has {manifest}",
        )
    recorded = doc.get("gates") or {}
    gates = {
        k: max(float(recorded.get(k, 0) or 0), v) for k, v in DEFAULT_GATES.items()
    }
    ok, why = calibration_passes(doc, gates)
    if not ok:
        return False, f"{p.name}: {why}"
    if not doc.get("passed"):
        return False, f"{p.name} records passed=false"
    return True, "calibrated"


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    return pearson(_ranks(xs), _ranks(ys))


def weighted_kappa(
    a: Sequence[int], b: Sequence[int], categories: Sequence[int] = (1, 2, 3, 4, 5)
) -> float | None:
    """Quadratic-weighted Cohen's kappa between two raters on an ordinal scale."""
    if len(a) != len(b) or not a:
        return None
    cats = list(categories)
    idx = {c: i for i, c in enumerate(cats)}
    k = len(cats)
    n = len(a)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[idx[x]][idx[y]] += 1
    ra = [sum(obs[i][j] for j in range(k)) for i in range(k)]
    rb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2) if k > 1 else 0.0
            num += w * obs[i][j] / n
            den += w * (ra[i] * rb[j]) / (n * n)
    if den == 0:
        return None
    return 1.0 - num / den


def balanced_accuracy(pred: Sequence[bool], truth: Sequence[bool]) -> float | None:
    """Mean of recall on the positive and negative class; None if a class is absent."""
    if len(pred) != len(truth) or not pred:
        return None
    pos = [p for p, t in zip(pred, truth) if t]
    neg = [p for p, t in zip(pred, truth) if not t]
    if not pos or not neg:
        return None
    tpr = sum(1 for p in pos if p) / len(pos)
    tnr = sum(1 for p in neg if not p) / len(neg)
    return (tpr + tnr) / 2
