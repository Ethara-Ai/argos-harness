# Reward Change Log — REVERTED 2026-08-13 (original change record below)

**TL directive 2026-08-13 (relayed and confirmed by Anzar): full revert of
7514c58.** The composition is back to the corpus scorer's kappa-based form
(revert commit `0dc59e7`; `git log --grep="remove kappa"` finds both directions).

- `assay/` tree, both pilot fixtures, `test_assay_replay.py` (byte-identical
  corpus replay restored) and `test_bundle_structure.py` are **byte-identical
  to their pre-7514c58 state** (verified with `git diff` against `7514c58^`).
  `assay/agreement.py` (Cohen's kappa) is back; `test_assay_compose.py` and
  `SCORE_MATH.md` (both introduced by the reverted commit) are gone.
- **Known, accepted consequence** (explicitly acknowledged before reverting):
  with a single judge, kappa is undefined and `channel_weight(None) → 0.0` —
  single-judge runs score the process channel on the 28 deterministic checks
  alone; the judge still runs and its verdicts are recorded, but they carry
  zero weight in the composed score. This restores corpus-format parity
  (`council` block, kappa fields in process.json/result.json).
- **All trajectories will be regenerated from scratch** under this scoring;
  the 2026-08-11→12 outputs (41-instance dapr batch, 31 scored bundles) were
  discarded — nothing delivered mixes the two formats.

Everything below is the ORIGINAL 2026-08-11 change record, kept as history.

---

# Reward Change Log — remove kappa, keep alpha (single-judge composition)

**Date:** 2026-08-11 · **Spec:** TL doc "Aurora Reward — Change Spec: Remove Kappa, Keep Alpha" · **Chosen weight:** `w = 1.0` (spec's recommended default; deterministic channel kept)

**Why:** the reward runs on a single LLM judge. Kappa (inter-judge agreement) is undefined with one judge, and `channel_weight(None) → 0.0` meant **the judge's verdicts were silently zero-weighted** — every single-judge run scored its process channel on the deterministic checks alone. The fix replaces the agreement-derived weight with a fixed, documented constant.

**What did NOT change:** the outcome score (`score_v2g`), all 28 deterministic checks, the rubric tally (dimension budgets, floor band, verdict-resolution ladder), alpha (`alpha_for_task`), the stratified `score_rl` form, the gate semantics, and every harness-owned score field. This is pinned by `tests/test_assay_replay.py`, which requires those channels to replay **byte-identically** against the reference corpus.

## How to revert

The whole change is one commit. `git log --oneline --grep="remove kappa"` to find it, then:

```bash
git revert <sha>          # restores all source, tests, fixtures and docs in one commit
```

The deleted `assay/agreement.py` is restored by the revert (its full content lives in git history at the parent of that commit).

---

## 1. `assay/compose.py` — the formula

**Before:**
```python
def channel_weight(kappa: float | None) -> float:
    return max(0.0, float(kappa)) if kappa is not None else 0.0

def process_score(*, det: float, rubric: float | None, kappa: float | None) -> float:
    w = 0.0 if rubric is None else channel_weight(kappa)
    return (det + w * (rubric or 0.0)) / (1.0 + w)
```

**After:**
```python
RUBRIC_WEIGHT = 1.0

def process_score(*, det: float, rubric: float | None) -> float:
    w = 0.0 if rubric is None else RUBRIC_WEIGHT
    return (det + w * (rubric or 0.0)) / (1.0 + w)
```

`eval_score()` / `rl_score()` lost their `kappa` parameter; `combine()`, `stratify()`, `alpha_for_task()`, `ALPHA_CAP`, `FLAT_STRATUM`, `MIN_MEANINGFUL_GAP` untouched.

## 2. `assay/cli.py` — the composer

- Removed `from .agreement import pooled_kappa` and the per-task `kappa = pooled_kappa(...)` computation in `_compose_group()`.
- `composition` dict: **removed** `"kappa"`, `"kappa_scope"`; `"rubric_weight"` is now the constant `RUBRIC_WEIGHT` (was `round(channel_weight(kappa), 4)`).
- `_score_md()` (final_score.md template): header line was `Judges: n=<N> (<members>), κ=<kappa>` → now `Judge: <model>` (members joined with `+` on legacy multi-member replays; `unjudged` when unjudged).

## 3. `assay/report.py` — the report

- Removed constants `W_DETERMINISTIC = 0.7` / `W_RUBRIC = 0.3` (the legacy fallback blend); the fallback now delegates to `compose.process_score` so the two paths cannot disagree.
- Removed properties `council_kappa`, `judge_observations`; `_council_block()` (n_judges/members/raw_agreement/kappa/pairs/status) replaced by `_judge_block()` → `{"model": "<judge>"}`.
- `to_dict()`: top-level key `"council"` → `"judge"`; `process` block dropped `"contested_items"` (a single judge cannot disagree with itself).
- `channel_weights` fallback now reports the fixed split `{deterministic: 1.0, rubric: 1.0, normalised_by: 2.0}` (was `{0.7, 0.3, 1.0}`).
- `rl` block "formula" text: "channel split is kappa-weighted" → "channel split is the fixed rubric weight".

## 4. `assay/writeback.py` — result.json merge

**Before:** `assay{alpha, kappa, rubric_weight, gate, stratum_size, council, status}` (council = member list).
**After:** `assay{alpha, rubric_weight, gate, stratum_size, judge, status}` (judge = model string).
The six `score_*` fields and the harness-owned `score`/`score_binary`/`score_continuous_v2` are unchanged.

## 5. `assay/rubric.py` — serialization only

`RubricReport.to_dict()` dropped `"judge_members"`, `"contested"`, `"dissent_filtered"`; per-item `"resolution": "<unanimous|anchor|majority|abstain_*>"` → `"abstained": <bool>`. **The scoring math and the verdict-resolution ladder (`aggregate`, truncation/citation/prerequisite abstentions) are untouched** — they degenerate naturally to unanimous with one judge and keep legacy 2-judge verdict stores replayable. `dissent_filtered`/`judge_spread`/`mean_agreement` remain as internal diagnostics for `assay validate`.

## 6. Deleted: `assay/agreement.py`

Cohen's kappa / council agreement / pooled kappa (117 lines). Only `cli.py` and `report.py` imported it. Restored automatically by `git revert`.

## 7. Artifact schema — old vs new

| Artifact | Before | After |
|---|---|---|
| `process.json` top level | `…, "council": {n_judges, members, n_items, raw_agreement, kappa, pairs, note, status}, …` | `…, "judge": {"model": "sonnet-5"}, …` (same position) |
| `process.json` composition | `{alpha, min_outcome_gap, kappa, kappa_scope, rubric_weight(=kappa-derived), process, process_stratified, stratum_size, score_eval, score_rl}` | `{alpha, min_outcome_gap, rubric_weight: 1.0, process, process_stratified, stratum_size, score_eval, score_rl}` |
| `process.json` process | had `contested_items` | removed |
| `process.json` detail.rubric | `judge_members[]`, `contested[]`, `dissent_filtered[]`, per-item `resolution` | removed; per-item `abstained` bool |
| `result.json` assay | `{alpha, kappa, rubric_weight, gate, stratum_size, council, status}` | `{alpha, rubric_weight, gate, stratum_size, judge, status}` |
| `final_score.md` | `Judges: n=2 (…), κ=0.7565` · `process = (det + 0.7565·rubric)/1.7565` | `Judge: sonnet-5` · `process = (det + 1·rubric) / 2` |

## 8. Tests

- `tests/test_assay_replay.py` — **re-scoped, deliberately.** Was: byte-identical regeneration of all four corpus bundles. Now: byte-identity for every unchanged channel (outcome, deterministic checks, rubric tally, verdicts, harness-owned scores) + exact recomputation of the composed fields under the new formula (channels rebuilt unrounded from the detail block) + new-schema asserts. The corpus keeps the old format, so full byte-identity is impossible by design — this is the accepted trade-off of the spec.
- `tests/test_bundle_structure.py` — ours-vs-corpus structure comparison now translates `council→judge` and carries an explicit `removed` key set; `assay` block pinned per-root (ours new list, corpus old list); `Judges?:` normalization.
- `tests/test_assay_compose.py` — **new**: pins `w = 1.0`, the simple-average process, the spec's worked example (0.2195/0.2349 → 0.2272), gate semantics, alpha derivation, absence of `assay.agreement`, and the new assay-block shape.
- `tests/fixtures/milo_bundles/{9a959823…,150c282e…}` — regenerated with the new scorer (`ASSAY_COUNCIL="sonnet-5=claude-sonnet-5"`); pilot semantics unchanged: 538 still voided by B4, 943 still caught by B1.
- `tests/test_milo_pilot_regression.py` — passes unmodified (its assertions are structural).
- Pre-existing failures NOT from this change (verified by stash-and-rerun): 11 tests in `test_anti_reward_hacking_wiring.py`, `test_instance_timeout.py`, `test_security_fixes.py` fail identically before and after.

## 9. Numeric effect (fixtures)

| Run | det | rubric | process before (w=0: det only) | process after (w=1) |
|---|--:|--:|--:|--:|
| tortoise-538 opus-4.8/run_1 | 0.102 | 0.415 | 0.102 | 0.258 |
| tortoise-943 opus-4.8/run_1 | 0.189 | 0.878 | 0.189 | 0.533 |

Both runs remain gated (`score_eval = score_rl = 0.0` — 538 voided by B4, 943 unverifiable via B1); the process measurement is now reported with the judge channel actually counted.
