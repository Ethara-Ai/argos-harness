# Reward Change Log — plain-average process score: kappa AND weight constant removed (2026-08-13)

**Date:** 2026-08-13 (evening) · **Directive:** Anzar, aligned with TL's expected artifact
(`**process** = (det + 1·rubric) / 2` shown as the required final_score.md row), tightened by
Anzar's explicit follow-up: *no weight constant either* — a dial that only ever multiplies by
one is dead weight. · **Supersedes** the REVERTED chapter below: the zero-weighting accepted
there is now resolved the other way.

**The formula:** `process = (det + rubric) / 2` — plain average, written directly, no
`RUBRIC_WEIGHT`, no `channel_weight(kappa)`, no kappa. An unjudged run (no rubric channel to
average) scores `process = det`, same fallback as both prior designs. Numerically identical to
7514c58's `w = 1.0` blend; the difference is that the weight *machinery* is gone from code and
artifacts, not just neutralized.

**How it was made:** `git revert 0dc59e7` (re-applying 7514c58: kappa removal, judge block,
`agreement.py` deleted) folded together with the RUBRIC_WEIGHT removal into this one commit.
Conflicts with the weight-ladder commit (`de18e85`, which landed between) resolved by unioning
the replay test's exemption sets and regenerating both pilot fixtures with the live scorer.

**Beyond the 7514c58 schema, this removes:**

| Where | 7514c58 had | Now |
|---|---|---|
| `assay/compose.py` | `RUBRIC_WEIGHT = 1.0`, weighted blend | `process_score` = plain average, no constant |
| `process.json` composition | `rubric_weight: 1.0` | key absent |
| `process.json` process | `weights: {deterministic, rubric, normalised_by}` | key absent |
| `result.json` assay block | `{alpha, rubric_weight, gate, stratum_size, judge, status}` | `{alpha, gate, stratum_size, judge, status}` |
| `final_score.md` | `**process** = (det + 1·rubric) / 2` | `**process** = (det + rubric) / 2` (`det (unjudged)` when unjudged) |
| `assay/report.py` | `channel_weights` property | removed (nothing to report — the average is self-describing) |

**What did NOT change:** the outcome score (`score_v2g`), all 28 deterministic checks and the
{1,3,5} weight ladder (`de18e85` untouched), hard-gate void semantics, the rubric tally
(dimension budgets, floor band, abstention ladder), alpha (`alpha_for_task`, cap 0.05), the
stratified `score_rl` form, every harness-owned score field, and all stored verdicts (the
bundle fingerprint excludes composition — no re-judging anywhere).

**Tests:** 37 green, including the 4-bundle corpus replay (`test_assay_replay.py`, exemption
set = union of single-judge + ladder paths; legacy 2-judge council stores still replay),
`test_assay_compose.py` re-pinned to the plain average and to the *absence* of
`RUBRIC_WEIGHT`/`channel_weight`, `test_bundle_structure.py` (removed-keys set gains
`composition.rubric_weight` + `process.weights.*`), pilot regression and weight-ladder suites
unmodified.

**Numeric effect** (both pilots stay gated — rewards 0.0 — but process now counts the judge):

| Run | det | rubric | process before (det only) | process now |
|---|--:|--:|--:|--:|
| tortoise-538 opus-4.8/run_1 | 0.1250 | 0.4147 | 0.1250 | 0.2699 (B4-voided) |
| tortoise-943 opus-4.8/run_1 | 0.2222 | 0.8779 | 0.2222 | 0.5500 (B1-gated) |
| smoke dapr-1351 (edd779ae) | 0.1875 | 0.4029 | 0.1875 | 0.2952, score_eval 0.0295 |
| smoke dapr-1638 (6816e922) | 0.1250 | 0.4911 | 0.1250 | 0.3080, score_eval 0.0308 |

Smoke bundles rescored in place from stored verdicts. **Corpus-format divergence is accepted**
(the very concern that drove the 0dc59e7 revert): council/kappa fields no longer appear in our
artifacts; the replay test asserts equivalence channel-by-channel instead of byte-identity, as
under 7514c58. Scores are again not comparable across eras — nothing delivered mixes formats
(all pre-change outputs were already discarded for regeneration).

## How to revert

One commit. `git log --oneline --grep="plain-average"`, then `git revert <sha>` — restores the
kappa-era scorer, `agreement.py`, both fixtures and all tests together.

---

# Reward Change Log — weight ladder: det check weights onto {1, 3, 5} (2026-08-13)

**Date:** 2026-08-13 · **Directive (TL, relayed by Anzar):** every weight in delivered artifacts must lie in {-5, -3, -1, 1, 3, 5}. Rubric items already comply (`assay/lint.py ALLOWED_WEIGHTS`); this change brings the deterministic check channel onto the positive half of the same ladder. **Forward-only:** nothing already delivered is rescored.

**What changed** (all in `assay/deterministic.py`):

| check | before | after |
|---|--:|--:|
| `E2-req` (`REQUIREMENT_WEIGHT`) | 8 | **5** |
| `C6-no-unknown-breaks` | 2 | **1** |
| `F1-no-out-of-scope-churn` | 2 | **1** |
| `E3-issue-reach` | declared 1, **emitted 6** (live mismatch) | **3** both |
| discount for target-test-observed sites/requirements | `round(w × 0.5)` → 4 and 2 | `DISCOUNT_STEP = {5: 3, 3: 1}` (one rung down; multiply-and-round lands between rungs: `round(5·0.5) == 2`) |

Every weight the engine can now emit is in {0, 1, 3, 5}; 0 is not a weight but the existing "row excluded" marker (an E1 row a scored E2 requirement already entails). Pinned by `tests/test_weight_ladder.py`.

**What did NOT change:** the outcome score (`score_v2g`), all check ids/gates/verdicts/details (28 families, 16 hard / 12 soft), hard-gate void semantics, the rubric channel (its per-item continuous weights are dimension-budget-rescaled — a different vocabulary, untouched), kappa/alpha/composition formulas, every harness-owned score field, judge verdicts (the bundle fingerprint excludes the checks table, so no re-judging). `soft_score` is a weighted ratio, so only the relative masses moved: E2 stays the dominant weight (5 vs hygiene 1), slightly less dominant than at 8.

**Consequences accepted:** `score_deterministic`/`score_process`/`score_eval`/`score_rl` shift for everything scored from now on (fixtures: U538 det 0.1017 → 0.125, U943 det 0.1887 → 0.2222); scores are not comparable across the weight eras. `tests/test_assay_replay.py` now exempts exactly the det-derived value paths (weights, soft_score, and their downstream composition numbers) and instead asserts the ladder invariant — check ids/gates/verdicts/details and the whole rubric/outcome side remain byte-pinned to the corpus. `tests/test_bundle_structure.py` compares the checks table to the corpus weight-exempt. The 33 old-era bundles under `argos_bundles/` and their `rubrics.json`/`process.json` keep the old numbers on purpose.

## How to revert

One commit. `git log --oneline --grep="weight ladder"`, then `git revert <sha>` (restores engine, tests and both pilot fixtures together).

---

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
- `tests/fixtures/argos_bundles/{9a959823…,150c282e…}` — regenerated with the new scorer (`ASSAY_COUNCIL="sonnet-5=claude-sonnet-5"`); pilot semantics unchanged: 538 still voided by B4, 943 still caught by B1.
- `tests/test_argos_pilot_regression.py` — passes unmodified (its assertions are structural).
- Pre-existing failures NOT from this change (verified by stash-and-rerun): 11 tests in `test_anti_reward_hacking_wiring.py`, `test_instance_timeout.py`, `test_security_fixes.py` fail identically before and after.

## 9. Numeric effect (fixtures)

| Run | det | rubric | process before (w=0: det only) | process after (w=1) |
|---|--:|--:|--:|--:|
| tortoise-538 opus-4.8/run_1 | 0.102 | 0.415 | 0.102 | 0.258 |
| tortoise-943 opus-4.8/run_1 | 0.189 | 0.878 | 0.189 | 0.533 |

Both runs remain gated (`score_eval = score_rl = 0.0` — 538 voided by B4, 943 unverifiable via B1); the process measurement is now reported with the judge channel actually counted.
