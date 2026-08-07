"""Rubric lint.

WildClawBench states the non-overlap rule well and then never enforces it: the
overlap auditor at ``system_prompts/testgen_rubric_overlap.md`` is loaded by no
code path, and ``task_parser.py`` json-loads the rubric without validating a
single field, silently coercing a malformed weight to 1.0. That is the failure
this module exists to prevent. Overlap and sloppy rubric text are build errors
here, not review notes.

Findings carry a severity. ``ERROR`` fails the bundle. ``WARN`` is advisory and
covers the criteria that can only be checked heuristically, which are called out
as heuristic rather than dressed up as decisions.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .truth import JUDGE_SECTIONS, _split_sections


# Paths that reveal the reference solution or the graded test set. packet.py keeps
# the literal fix.patch out of the judge's view on purpose; a path to it inside
# judge-visible prose reintroduces grading-by-similarity through the back door.
ORACLE_PATH_RE = re.compile(
    r"solution/|fix\.patch|test\.patch|solve\.sh|/home/test|tests/config"
)

ALLOWED_WEIGHTS = frozenset({5, 3, 1, -1, -3, -5})
# D2..D6 of the universal axis set (.work/METHODOLOGY.md §3), plus honesty as the
# guardrail home. D1 correctness is absent on purpose: it is the outcome grade and
# the process channel never re-judges it.
ALLOWED_DIMENSIONS = frozenset(
    {
        "issue_coverage",
        "scope_discipline",
        "verification",
        "adherence",
        "maintainability",
        "honesty",
    }
)
ALLOWED_TARGETS = frozenset({"trajectory", "final_diff", "verification_output"})
ALLOWED_MODES = frozenset({"deterministic", "judged"})
REQUIRED_FIELDS = (
    "id",
    "dimension",
    "weight",
    "evaluation_target",
    "criterion",
    "evaluation_rule",
    "evidence",
)

# Predicates whose truth is decidable from bytes. A criterion built only out of
# these is a pytest assertion wearing a rubric costume.
MECHANICAL_PREDICATES = (
    "exists",
    "is non-empty",
    "is empty",
    "count of",
    "number of",
    "equals",
    "matches the regex",
    "is present",
    "is absent",
    "file was created",
    "returns exit code",
    "is greater than",
    "is less than",
    "at least n",
)

# Phrasing that reads as machine-written. Rubric text is fed to a judge model and
# this register measurably shifts how such models grade, so it is an ERROR, not
# a style preference.
BANNED_PHRASES = (
    "leverage",
    "leverages",
    "leveraging",
    "robust",
    "robustly",
    "comprehensive",
    "comprehensively",
    "delve",
    "delves",
    "seamless",
    "seamlessly",
    "utilize",
    "utilizes",
    "utilizing",
    "furthermore",
    "moreover",
    "it is important to note",
    "plays a crucial role",
    "plays a vital role",
    "in today's",
    "landscape of",
    "testament to",
    "underscores",
    "showcase",
    "showcases",
    "intricate",
    "meticulous",
    "meticulously",
    "elevate",
    "elevates",
    "myriad",
    "realm of",
)

EM_DASHES = ("—", "–")

SIMILARITY_THRESHOLD = 0.80
MIN_CRITERION_WORDS = 8
MAX_CRITERION_WORDS = 70


@dataclass(frozen=True)
class Finding:
    severity: str  # "ERROR" | "WARN"
    item_id: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.item_id or '<bundle>'} {self.rule}: {self.message}"


@dataclass
class LintResult:
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]

    @property
    def ok(self) -> bool:
        return not self.errors


def lint_rubric(rubric_path: str | Path, task_tokens: Iterable[str] = ()) -> LintResult:
    """Validate a rubrics.json.

    ``task_tokens`` are identifiers unique to the task (source file basenames,
    symbol names). They drive the task-specificity heuristic.
    """
    path = Path(rubric_path)
    findings: list[Finding] = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) or {}
    except ValueError as exc:
        return LintResult(
            [
                Finding(
                    "ERROR", "", "R000-parse", f"rubrics.json is not valid JSON: {exc}"
                )
            ]
        )

    items = doc.get("items") or []
    if not items:
        return LintResult(
            [Finding("ERROR", "", "R001-empty", "rubric declares no items")]
        )

    seen_ids: set[str] = set()
    tokens = {t.lower() for t in task_tokens if t}
    # `requires` may name an item declared later, and the shared preamble is
    # merged in at scoring time, so a bare id is resolved against both.
    known_ids = {str(i.get("id")) for i in items} | _preamble_ids()

    for item in items:
        iid = str(item.get("id") or "<no-id>")
        findings.extend(_check_fields(iid, item))
        findings.extend(_check_weight(iid, item))
        findings.extend(_check_enums(iid, item))
        findings.extend(_check_prose(iid, item))
        findings.extend(_check_mechanical(iid, item))
        findings.extend(_check_narration(iid, item))
        findings.extend(_check_mode_and_requires(iid, item, known_ids))
        if iid in seen_ids:
            findings.append(Finding("ERROR", iid, "R002-dup-id", "duplicate item id"))
        seen_ids.add(iid)

    findings.extend(_check_sibling_overlap(items))
    findings.extend(_check_bundle(items, tokens))
    return LintResult(findings)


# A criterion that names the route rewards reproducing the reference rather than
# solving the problem. These patterns catch the four ways a route gets named:
# a path, a call, a code fragment in backticks or quotes, and a bare identifier
# in camelCase, snake_case or Type.member form.
_FORCE_PATTERNS = (
    (
        "path",
        re.compile(r"\b[\w./-]+\.(go|py|js|ts|tsx|rs|c|h|cpp|java|rb|json|yml|yaml)\b"),
    ),
    ("call", re.compile(r"\b[A-Za-z_][\w.]*\s*\([^)]*\)")),
    ("quoted-code", re.compile(r"`[^`]+`|\"[a-z][\w.-]*[-_.][\w.-]*\"")),
    (
        "identifier",
        re.compile(r"\b(?:[a-z]+[A-Z]\w*|\w+_\w+|[A-Z]\w*\.\w+|[a-z]\w*\.[A-Z]\w*)\b"),
    ),
)

# Ordinary prose that the identifier pattern would otherwise flag.
_FORCE_ALLOW = re.compile(
    r"^(e\.g|i\.e|etc|vs|truth\.md|rubrics?\.json|process\.json|README)$", re.I
)


# A bare lowercase local such as `run`, `file` or `diff` is a word criteria use
# in ordinary prose. Only a compound or long name is one a criterion would
# mention deliberately, so only those are treated as naming the route.
_DISTINCTIVE_RE = re.compile(r"[A-Z]|_|^\w{8,}$")


_NAME_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")

# Words that carry no identity on their own, so a name made only of these is
# never treated as one the task asked for.
_NAME_NOISE = frozenset(
    {
        "api",
        "url",
        "uri",
        "wrapper",
        "helper",
        "util",
        "utils",
        "impl",
        "base",
        "get",
        "set",
        "new",
        "make",
        "build",
        "run",
        "do",
        "handler",
        "manager",
        "service",
        "client",
        "config",
        "options",
        "opts",
        "data",
        "value",
        "item",
        "list",
        "map",
        "str",
        "int",
        "id",
        "key",
        "name",
        "type",
        "class",
        "func",
        # Structural qualifiers: they modify a name without naming anything.
        "default",
        "min",
        "max",
        "shared",
        "common",
        "internal",
        "private",
        "public",
        "global",
        "local",
        "const",
        "static",
        "path",
        "dir",
        "file",
        "chat",
        "model",
    }
)


_PROSE_WORD_RE = re.compile(r"^[a-z]{6,}$")


def reads_as_prose_word(name: str) -> bool:
    """True for a plain lowercase noun that prose can legitimately use.

    The reference introducing a variable called `denominator` does not make the
    word off limits while explaining a rounding bug: every correct solution has
    one, whatever it is called. An underscored, camelCase or SHOUTING name
    cannot be read as anything but the author's own choice. The cost of this
    line is that a single lowercase invented helper name would pass; the cost of
    not drawing it was bundles with no reference account at all.
    """
    return bool(_PROSE_WORD_RE.match(name or ""))


def named_by_spec(name: str, spec_text: str) -> bool:
    """True when the task's own words already ask for this identifier.

    An introduced name is normally a free choice, so naming it pins the route.
    A feature request inverts that: when the issue says "add support for google
    scholar querying", GoogleScholarAPIWrapper is the deliverable. The test is
    whether the name's distinctive words all appear in the task's own text.
    """
    if not spec_text:
        return False
    parts = [p.lower() for p in _NAME_PART_RE.findall(name)]
    distinctive = [p for p in parts if len(p) > 2 and p not in _NAME_NOISE]
    if not distinctive:
        return False
    low = spec_text.lower()
    return all(p in low for p in distinctive)


def route_forcing_findings(text: str, fix_patch: str, spec_text: str = "") -> list[str]:
    """Names in this wording that the reference patch invents.

    Sound where the regex is not. Naming the defective function, an environment
    variable or an input the code must handle is naming the task; only a name the
    reference introduces is a choice a correct solution could have made
    differently. Inspection of the 16 items the regex flagged found 12 of 13
    named pre-existing repository entities.
    """
    from .metamorphic import introduced_identifiers

    out = []
    for name in sorted(introduced_identifiers(fix_patch)):
        if not _DISTINCTIVE_RE.search(name):
            continue
        if not re.search(rf"\b{re.escape(name)}\b", text or ""):
            continue
        if named_by_spec(name, spec_text) or reads_as_prose_word(name):
            continue
        out.append(f"introduced:{name}")
    return out


def method_forcing_findings(text: str) -> list[str]:
    """Ways this wording pins the solution to one route."""
    out = []
    for label, rx in _FORCE_PATTERNS:
        for m in rx.finditer(text or ""):
            token = m.group(0).strip()
            if _FORCE_ALLOW.match(token.strip('`"')):
                continue
            out.append(f"{label}:{token[:48]}")
    return out


def lint_truth(truth_path: str | Path) -> LintResult:
    """Reject an oracle path in judge-visible prose.

    truth.md is what the judge reads, so a pointer to the answer in it defeats
    the packet's own exclusion of the patch. The site table is linted separately
    by lint_pytest_spec.
    """
    path = Path(truth_path)
    if not path.is_file():
        return LintResult([Finding("ERROR", "", "R000-parse", f"{path} is not a file")])
    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text)
    findings: list[Finding] = []

    for name in JUDGE_SECTIONS:
        body = sections.get(name)
        if not body:
            continue
        for m in ORACLE_PATH_RE.finditer(body):
            findings.append(
                Finding(
                    "ERROR",
                    name,
                    "R080-oracle-path-in-judge-context",
                    f"judge-visible section names oracle path '{m.group(0)}'; "
                    "the judge must grade from prose, not from a pointer to the answer",
                )
            )
    return LintResult(findings)


def lint_pytest_spec(path: str | Path) -> LintResult:
    """Validate the deterministic manifest.

    Probes are literal source lines carrying quotes, backslashes and triple
    quotes, so a malformed manifest is the failure that reads as a bad run
    rather than as a broken bundle. It has to be caught here, not at scoring.
    """
    from .deterministic import GENERIC_FAMILIES

    findings: list[Finding] = []
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return LintResult(
            [
                Finding(
                    "ERROR",
                    "",
                    "R081-spec-parse",
                    f"pytest.json is not readable JSON: {exc}",
                )
            ]
        )

    if not isinstance(doc.get("checks"), dict) or not isinstance(
        doc.get("sites"), list
    ):
        findings.append(
            Finding(
                "ERROR",
                "",
                "R081-spec-parse",
                "pytest.json needs a 'checks' mapping and a 'sites' list",
            )
        )
        return LintResult(findings)

    for cid in doc["checks"]:
        if cid not in GENERIC_FAMILIES:
            findings.append(
                Finding(
                    "ERROR",
                    cid,
                    "R082-unknown-check",
                    "declares a check the engine cannot emit",
                )
            )

    seen: set[str] = set()
    for site in doc["sites"]:
        if not site.get("path"):
            findings.append(Finding("ERROR", "", "R083-site-shape", "site has no path"))
        for req in site.get("requirements") or []:
            rid = req.get("id") or ""
            if not rid:
                findings.append(
                    Finding(
                        "ERROR",
                        site.get("path", ""),
                        "R083-site-shape",
                        "requirement has no id",
                    )
                )
            elif rid in seen:
                findings.append(
                    Finding(
                        "ERROR",
                        rid,
                        "R084-duplicate-requirement",
                        "requirement id is used twice",
                    )
                )
            seen.add(rid)
            probe = req.get("probe") or {}
            if not probe.get("any_of") and not probe.get("none_of"):
                findings.append(
                    Finding(
                        "WARN",
                        rid,
                        "R085-unmeasurable",
                        "no probe, so every run abstains on it",
                    )
                )
    return LintResult(findings)


def _check_fields(iid: str, item: dict[str, Any]) -> list[Finding]:
    out = []
    for f in REQUIRED_FIELDS:
        if not item.get(f):
            out.append(
                Finding(
                    "ERROR", iid, "R010-missing-field", f"missing required field '{f}'"
                )
            )
    return out


def _preamble_ids() -> set[str]:
    p = Path(__file__).resolve().parent / "preamble.json"
    if not p.is_file():
        return set()
    return {str(i.get("id")) for i in json.loads(p.read_text()).get("items") or []}


def _check_mode_and_requires(
    iid: str, item: dict[str, Any], known_ids: set[str]
) -> list[Finding]:
    out = []
    mode = item.get("mode", "judged")
    if mode not in ALLOWED_MODES:
        out.append(
            Finding(
                "ERROR",
                iid,
                "R090-mode",
                f"unknown mode {mode!r}, allowed {sorted(ALLOWED_MODES)}",
            )
        )
    req = item.get("requires")
    if req:
        if req == iid:
            out.append(
                Finding(
                    "ERROR", iid, "R091-requires-self", "an item cannot license itself"
                )
            )
        elif req not in known_ids:
            out.append(
                Finding(
                    "ERROR",
                    iid,
                    "R092-requires-unknown",
                    f"requires {req!r}, which no item declares",
                )
            )
    return out


def _check_weight(iid: str, item: dict[str, Any]) -> list[Finding]:
    w = item.get("weight")
    if not isinstance(w, int) or isinstance(w, bool):
        return [
            Finding(
                "ERROR",
                iid,
                "R020-weight-type",
                f"weight must be an integer, got {type(w).__name__}",
            )
        ]
    if w not in ALLOWED_WEIGHTS:
        return [
            Finding(
                "ERROR",
                iid,
                "R021-weight-set",
                f"weight {w} is outside {sorted(ALLOWED_WEIGHTS)}; "
                "rejected rather than coerced",
            )
        ]
    return []


def _check_enums(iid: str, item: dict[str, Any]) -> list[Finding]:
    out = []
    d = item.get("dimension")
    if d and d not in ALLOWED_DIMENSIONS:
        out.append(
            Finding(
                "ERROR",
                iid,
                "R030-dimension",
                f"unknown dimension '{d}', allowed {sorted(ALLOWED_DIMENSIONS)}",
            )
        )
    t = item.get("evaluation_target")
    if t and t not in ALLOWED_TARGETS:
        out.append(
            Finding(
                "ERROR",
                iid,
                "R031-target",
                f"unknown evaluation_target '{t}', allowed {sorted(ALLOWED_TARGETS)}",
            )
        )
    return out


_QUOTED_RE = re.compile(r'"[^"]*"')


def _check_prose(iid: str, item: dict[str, Any]) -> list[Finding]:
    out = []
    for field in ("criterion", "evaluation_rule"):
        text = str(item.get(field) or "")
        # The rule polices authored register, and a quoted issue title is the
        # milestone's own words. Editing one to satisfy a style rule would make
        # the criterion cite a title that does not exist upstream.
        outside = _QUOTED_RE.sub(" ", text)
        for dash in EM_DASHES:
            if dash in outside:
                out.append(
                    Finding(
                        "ERROR",
                        iid,
                        "R040-em-dash",
                        f"{field} contains an em or en dash",
                    )
                )
                break
        low = outside.lower()
        for phrase in BANNED_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                out.append(
                    Finding(
                        "ERROR",
                        iid,
                        "R041-register",
                        f"{field} contains machine-register phrase '{phrase}'",
                    )
                )
    words = len(str(item.get("criterion") or "").split())
    if words and words < MIN_CRITERION_WORDS:
        out.append(
            Finding(
                "WARN",
                iid,
                "R042-terse",
                f"criterion is {words} words; likely not self-contained",
            )
        )
    if words > MAX_CRITERION_WORDS:
        out.append(
            Finding(
                "ERROR",
                iid,
                "R043-verbose",
                f"criterion is {words} words, over {MAX_CRITERION_WORDS}; "
                "long criteria bundle several judgements and should be split",
            )
        )
    return out


def _check_mechanical(iid: str, item: dict[str, Any]) -> list[Finding]:
    """A criterion made only of decidable predicates belongs in the pytest channel."""
    crit = str(item.get("criterion") or "").lower()
    hits = [p for p in MECHANICAL_PREDICATES if p in crit]
    if not hits:
        return []
    judgment = str(item.get("evaluation_rule") or item.get("judgment") or "").strip()
    if len(judgment.split()) < MIN_CRITERION_WORDS:
        return [
            Finding(
                "ERROR",
                iid,
                "R050-redundant",
                f"criterion uses decidable predicate(s) {hits} without a "
                "judgment clause naming the residual; this belongs in the "
                "deterministic channel",
            )
        ]
    return [
        Finding(
            "WARN",
            iid,
            "R051-near-mechanical",
            f"criterion uses decidable predicate(s) {hits}; the judgment "
            "clause is present, confirm the residual is real",
        )
    ]


# Sources that record what the run did. `trajectory.reasoning` is what it said it
# did, which under RL is the cheapest thing to fake, so it cannot on its own earn
# weight. Guardrails are exempt: G1 measures the narration itself and only ever
# subtracts.
ACTION_EVIDENCE = frozenset(
    {
        "trajectory.tool_calls",
        "trajectory.observations",
        "final_diff",
        "verification_output",
    }
)


def _check_narration(iid: str, item: dict[str, Any]) -> list[Finding]:
    try:
        weight = int(item.get("weight", 0))
    except (TypeError, ValueError):
        return []
    if weight <= 0:
        return []
    evidence = set(item.get("evidence") or ())
    if evidence & ACTION_EVIDENCE:
        return []
    return [
        Finding(
            "ERROR",
            iid,
            "R044-narration-only",
            "positively weighted item rests only on what the run said; cite an "
            f"action source ({', '.join(sorted(ACTION_EVIDENCE))})",
        )
    ]


def _check_sibling_overlap(items: list[dict[str, Any]]) -> list[Finding]:
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.get("dimension") != b.get("dimension"):
                continue
            ta, tb = _norm(a.get("criterion")), _norm(b.get("criterion"))
            if not ta or not tb:
                continue
            ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                out.append(
                    Finding(
                        "ERROR",
                        f"{a.get('id')}~{b.get('id')}",
                        "R060-sibling-overlap",
                        f"same dimension '{a.get('dimension')}' and criterion similarity "
                        f"{ratio:.2f} >= {SIMILARITY_THRESHOLD}",
                    )
                )
    return out


def _check_bundle(items: list[dict[str, Any]], tokens: set[str]) -> list[Finding]:
    out = []
    if not any(isinstance(i.get("weight"), int) and i["weight"] < 0 for i in items):
        out.append(
            Finding(
                "ERROR",
                "",
                "R070-no-guardrail",
                "rubric has no negative-weight guardrail item, so no run "
                "can be penalised for how it reached its result",
            )
        )
    if not any(isinstance(i.get("weight"), int) and i["weight"] > 0 for i in items):
        out.append(
            Finding(
                "ERROR",
                "",
                "R071-no-positive",
                "rubric has no positive-weight item, so the denominator is empty",
            )
        )
    if tokens:
        # The shared items are generic on purpose and are copied into every
        # rubric, so counting them would make the ratio a function of how many
        # dimensions the preamble covers rather than of the authoring.
        shared = _preamble_ids()
        authored = [i for i in items if str(i.get("id")) not in shared]
        specific = 0
        for i in authored:
            blob = f"{i.get('criterion', '')} {i.get('evaluation_rule', i.get('judgment', ''))}".lower()
            if any(t in blob for t in tokens):
                specific += 1
        if specific * 2 < len(authored):
            out.append(
                Finding(
                    "WARN",
                    "",
                    "R072-generic",
                    f"only {specific}/{len(items)} items name something specific "
                    "to this task; generic items belong in a shared preamble",
                )
            )
    return out


_WS = re.compile(r"\s+")


def _norm(text: Any) -> str:
    return _WS.sub(" ", str(text or "")).strip().lower()
