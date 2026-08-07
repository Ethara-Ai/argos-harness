"""Test whether a criterion is pinned to one route.

Metamorphic relation: for a correct solution S, a semantics-preserving
transformation T and a criterion C, C(S) must equal C(T(S)). A criterion that
flips is rewarding the reference's spelling rather than the behaviour.

Preservation is witnessed, never assumed. Of 39 published "semantic-preserving"
transformations, a manual check found 23 actually changed semantics
(arXiv:2503.23448), so a transformation counts here only when the task's own
target tests still pass after it. The task suite is the oracle for the oracle.

The transformations rename or move only what the patch itself introduces. A
correct solution could have chosen any name for those, so a criterion may not
depend on the choice; names the patch merely calls belong to the library or the
pre-existing code and are part of the specification, not the route.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable, Iterable


__all__ = [
    "introduced_identifiers",
    "rename_identifiers",
    "route_dependent_probes",
    "TRANSFORMS",
]

# A definition the patch adds, across the languages in the corpus: Go/Rust/C
# `func|fn name(`, Python `def name(`, JS/TS `function name(` or `const name =`,
# Java/C++ `Type name(`, and plain assignment `name :=` / `name =`.
_WORD_RE = re.compile(r"[A-Za-z_]\w*")

_DEF_RE = re.compile(
    r"^\+\s*(?:"
    r"(?:func|fn|def|function|class|struct|type|interface)\s+(?P<a>[A-Za-z_]\w*)"
    r"|(?:const|let|var|val)\s+(?P<b>[A-Za-z_]\w*)"
    r"|(?P<c>[A-Za-z_]\w*)\s*:?=[^=]"
    r")",
    re.M,
)


def introduced_identifiers(patch_text: str) -> set[str]:
    return set(_introduced(patch_text or ""))


@lru_cache(maxsize=2)
def _introduced(patch_text: str) -> frozenset[str]:
    """Names the patch invents, excluding anything already visible in the file.

    A name occurring in a context or removed line existed before the change, so
    renaming it would not be semantics-preserving and naming it in a criterion is
    naming the task. Without this exclusion ordinary locals such as `run`, `file`
    and `ast` counted as introduced, and every criterion saying "the run ..." was
    flagged.
    """
    text = patch_text or ""
    # `\bname\b` over the pre-image is word-token membership, so the tokens are
    # collected once. Scanning per candidate name made this quadratic in patch
    # size, which is what made radare2's 8.2 MB reference unauthorable.
    prior = {
        w
        for ln in text.splitlines()
        if not ln.startswith("+") or ln.startswith("+++")
        for w in _WORD_RE.findall(ln)
    }
    out = set()
    for m in _DEF_RE.finditer(text):
        name = m.group("a") or m.group("b") or m.group("c")
        if not name or len(name) <= 2 or name in prior:
            continue
        out.add(name)
    return frozenset(out)


def rename_identifiers(text: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return text


def _shadow_rename(text: str) -> str:
    names = introduced_identifiers(text)
    return rename_identifiers(text, {n: f"zz{i}" for i, n in enumerate(sorted(names))})


def _reflow_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text)


def _strip_comments(text: str) -> str:
    return re.sub(r"\s*(?://|#)\s*.*$", "", text, flags=re.M)


TRANSFORMS: tuple[Callable[[str], str], ...] = (
    _shadow_rename,
    _reflow_whitespace,
    _strip_comments,
)


def route_dependent_probes(patch_text: str, probes: Iterable[str]) -> list[str]:
    """Probes that match the reference but stop matching a transformed one."""
    text = patch_text or ""
    candidates = [p for p in probes if p in text]
    if not candidates:
        return []
    return [p for p in candidates if any(p not in twin for twin in _twins(text))]


@lru_cache(maxsize=2)
def _twins(text: str) -> tuple[str, ...]:
    """The transformed copies of one patch, memoized.

    A transform rewrites the whole patch and never depends on the probe, yet the
    generator asks about probes one at a time while deriving them. Recomputing
    per question made authoring radare2's 7.8 MB reference take over four
    minutes; the cache holds one patch, since callers work a bundle at a time.
    """
    return tuple(t(text) for t in TRANSFORMS)
