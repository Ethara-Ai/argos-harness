"""Bind a set of verdicts to the bundle version that produced them.

Every invalid result this session was a version skew that left no trace: a
council judged against a truth.md a regeneration had reverted to a placeholder,
and an earlier pass was scored against a rubric that had since gained six items.
Both produced complete-looking artifacts. The fingerprint covers exactly what a
judge sees - the graded items and the judge-visible prose - so a change to
either invalidates the verdicts, while re-certifying or re-emitting tests does
not.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path


FINGERPRINT_KEY = "bundle_fingerprint"

# The corpus generator (argos-samples) computed the prose digest over the
# PRE-rename section names — three of which never exist in a shipped TRUTH.md,
# so they digest as "". Reproduced verbatim here (verified against corpus
# fingerprint f56ae50300f4f8ae, bundle 016372a9): changing this list
# invalidates every recorded corpus verdict. Deliberately decoupled from
# truth.JUDGE_SECTIONS, which governs what the judge actually reads.
_PROSE_SECTIONS = (
    "Defect",
    "Root cause",
    "Pseudocode",
    "Optimal step sequence",
    "Anti-patterns",
)

_ITEM_FIELDS = (
    "id",
    "dimension",
    "weight",
    "evaluation_target",
    "criterion",
    "evaluation_rule",
    "evidence",
    "mode",
    "requires",
)


def _items_digest(rubric_path: Path) -> str:
    try:
        doc = json.loads(rubric_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    rows = [
        [str(i.get(f, "")) for f in _ITEM_FIELDS]
        for i in sorted(doc.get("items") or [], key=lambda i: str(i.get("id")))
    ]
    return json.dumps(rows, sort_keys=True)


def _prose_digest(truth_path: Path) -> str:
    try:
        text = truth_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    from .truth import _split_sections

    sections = _split_sections(text)
    return json.dumps(
        {k: " ".join((sections.get(k) or "").split()) for k in sorted(_PROSE_SECTIONS)},
        sort_keys=True,
    )


_SCORER_SOURCES = ("assay/deterministic.py", "assay/compose.py", "assay/report.py")


@lru_cache(maxsize=1)
def scorer_stamp() -> str:
    """A digest of the code that turns checks and verdicts into a score.

    Deliberately not part of ``bundle_fingerprint``: a judge's answer does not
    depend on the checker, so changing one must not invalidate a verdict. It
    does invalidate the *score*, and nothing recorded that - three checkers
    changed in a single session and every artifact went on reading as current.
    """
    h = hashlib.sha256()
    root = Path(__file__).resolve().parent.parent
    for rel in _SCORER_SOURCES:
        p = root / rel
        h.update(p.read_bytes() if p.is_file() else b"")
    return h.hexdigest()[:16]


def bundle_fingerprint(task) -> str:
    """Covers exactly what a judge sees: the graded items and the visible prose."""
    blob = (
        _items_digest(Path(task.rubric_path))
        + "\x00"
        + _prose_digest(Path(task.truth_path))
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def check_verdict_fingerprint(task, recorded: str | None) -> str | None:
    """The reason these verdicts cannot be scored, or None if they can."""
    current = bundle_fingerprint(task)
    if recorded is None:
        return (
            f"verdicts carry no bundle fingerprint, so they cannot be shown to "
            f"match rubrics.json and truth.md as they stand ({current}). "
            f"Re-judge this task."
        )
    if recorded != current:
        return (
            f"verdicts were produced against bundle {recorded} but the bundle "
            f"is now {current}; the rubric or the judge-visible prose changed. "
            f"Re-judge this task."
        )
    return None


def recorded_fingerprint(verdicts: str | Path) -> str | None:
    """The fingerprint a verdict file or directory agrees on, else None."""
    p = Path(verdicts)
    files = [p] if p.is_file() else sorted(p.glob("*.jsonl"))
    seen = set()
    for fh in files:
        for line in fh.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get(FINGERPRINT_KEY))
            except ValueError:
                continue
    return seen.pop() if len(seen) == 1 else None
