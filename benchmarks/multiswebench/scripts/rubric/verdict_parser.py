"""Judge-response parsing — pure, tolerant, and it never raises.

The judge is asked to answer each criterion on its own line block::

    R3. <criterion echoed>
    [[RATIONALE: ...]]
    [[SATISFIED: Yes]]
    [[TRUNCATION_AFFECTED: No]]

Parsing keys on OUR ``R<n>`` numbers found at line starts — never on echoed
criterion text — so paraphrasing, renumbering order, or markdown decoration
cannot mis-attribute a verdict. A criterion whose verdict cannot be decided
unambiguously ABSTAINS; the parser must never guess and never crash (a judge
response is untrusted input).
"""

from __future__ import annotations

import re
from typing import Any


# Line-start anchor: optional markdown decoration (#, *, -, >, "3." / "3)")
# then the bare R-number. Line-start-only keeps an "R2" mentioned inside a
# rationale sentence from opening a new segment.
_ANCHOR_RE = re.compile(
    r"^[^\S\n]*(?:[#>*\-]+[^\S\n]*)?(?:\d+[.)][^\S\n]*)?(R\d+)\b",
    re.MULTILINE,
)

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")

_SATISFIED_RES = (
    # canonical double-bracket
    (re.compile(r"\[\[\s*SATISFIED\s*:\s*(yes|no)\s*\]\]", re.IGNORECASE), None),
    # single-bracket fallback
    (
        re.compile(r"(?<!\[)\[\s*SATISFIED\s*:\s*(yes|no)\s*\](?!\])", re.IGNORECASE),
        "single_bracket",
    ),
    # bare-label fallback, own line
    (
        re.compile(
            r"^[^\S\n]*SATISFIED\s*:\s*(yes|no)\b", re.IGNORECASE | re.MULTILINE
        ),
        "bare_label",
    ),
)
_TRUNCATION_RES = (
    (
        re.compile(r"\[\[\s*TRUNCATION_AFFECTED\s*:\s*(yes|no)\s*\]\]", re.IGNORECASE),
        None,
    ),
    (
        re.compile(
            r"(?<!\[)\[\s*TRUNCATION_AFFECTED\s*:\s*(yes|no)\s*\](?!\])", re.IGNORECASE
        ),
        "single_bracket",
    ),
    (
        re.compile(
            r"^[^\S\n]*TRUNCATION_AFFECTED\s*:\s*(yes|no)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        "bare_label",
    ),
)
_RATIONALE_RE = re.compile(
    r"\[\[?\s*RATIONALE\s*:\s*(.*?)\]\]?", re.IGNORECASE | re.DOTALL
)

_RATIONALE_CAP = 2000


def _sanitize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _ANSI_RE.sub("", text)


def _find_all(patterns, segment: str) -> tuple[list[str], str | None]:
    """All matched values for the first pattern tier that matches at all."""
    for pattern, warning in patterns:
        values = [m.group(1).strip().lower() for m in pattern.finditer(segment)]
        if values:
            return values, warning
    return [], None


def _abstain(reason: str) -> dict[str, Any]:
    return {
        "satisfied": None,
        "rationale": "",
        "truncation_affected": False,
        "abstain_reason": reason,
    }


def parse_judge_response(
    text: str | None, expected_numbers: list[str]
) -> dict[str, Any]:
    """Parse one judge response into per-criterion verdicts.

    Returns ``{"verdicts": {number: verdict}, "extra_numbers": [...],
    "warnings": [...]}`` with exactly one verdict per expected number (missing
    or undecidable → abstention, never absence).
    """
    try:
        return _parse(text, expected_numbers)
    except Exception as exc:  # noqa: BLE001 — untrusted input must never crash grading
        return {
            "verdicts": {n: _abstain("parse-miss") for n in expected_numbers},
            "extra_numbers": [],
            "warnings": [f"parser-exception:{type(exc).__name__}"],
        }


def _parse(text: str | None, expected_numbers: list[str]) -> dict[str, Any]:
    warnings: list[str] = []
    if not text or not text.strip():
        return {
            "verdicts": {n: _abstain("judge-error") for n in expected_numbers},
            "extra_numbers": [],
            "warnings": ["empty_response"],
        }

    clean = _sanitize(text)
    expected = set(expected_numbers)

    anchors = [(m.start(), m.group(1)) for m in _ANCHOR_RE.finditer(clean)]
    segments: dict[str, list[str]] = {}
    extra_numbers: list[str] = []
    for i, (pos, number) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(clean)
        if number in expected:
            segments.setdefault(number, []).append(clean[pos:end])
        elif number not in extra_numbers:
            extra_numbers.append(number)

    verdicts: dict[str, dict[str, Any]] = {}
    for number in expected_numbers:
        parts = segments.get(number)
        if not parts:
            verdicts[number] = _abstain("parse-miss")
            continue
        if len(parts) > 1:
            warnings.append(f"{number}:duplicate_sections")
        segment = "\n".join(parts)

        values, tier_warning = _find_all(_SATISFIED_RES, segment)
        if tier_warning:
            warnings.append(f"{number}:{tier_warning}")
        if not values:
            verdicts[number] = _abstain("parse-miss")
            continue
        if len(set(values)) > 1:
            warnings.append(f"{number}:conflicting_verdicts")
            verdicts[number] = _abstain("ambiguous-verdict")
            continue
        if len(values) > 1:
            warnings.append(f"{number}:duplicate_verdicts_same_value")
        satisfied = values[0] == "yes"

        trunc_values, _ = _find_all(_TRUNCATION_RES, segment)
        if not trunc_values:
            warnings.append(f"{number}:missing_truncation_field")
            truncation_affected = False
        elif len(set(trunc_values)) > 1:
            # Contradictory self-report: keep the conservative reading.
            warnings.append(f"{number}:conflicting_truncation")
            truncation_affected = True
        else:
            truncation_affected = trunc_values[0] == "yes"

        rationale_match = _RATIONALE_RE.search(segment)
        rationale = " ".join(
            (rationale_match.group(1) if rationale_match else "").split()
        )

        verdicts[number] = {
            "satisfied": satisfied,
            "rationale": rationale[:_RATIONALE_CAP],
            "truncation_affected": truncation_affected,
            "abstain_reason": None,
        }

    return {"verdicts": verdicts, "extra_numbers": extra_numbers, "warnings": warnings}
