# DOCKERFILE_SWAP — bundle `environment/Dockerfile` now ships the input task-folder Dockerfile

*Directive: TL, 2026-08-11 (relayed and confirmed by Anzar). Implemented 2026-08-11, commit `e512241`.*

## What changed

| | Before | After |
|---|---|---|
| Bundle `environment/Dockerfile` | Rendered from `task-template/environment/Dockerfile` (two-stage: python-fetch + `FROM <ECR per-PR image>` + referee install + `/workspace` symlink) | **Verbatim copy of the input task-folder Dockerfile** committed under `env_dockerfiles/{org}_m_{repo}/` (e.g. dapr's `Dockerfile.base`, XTLS's era-specific `Dockerfile{,1,2,3}` picked per PR via `map.json`) |
| Template render code | The only path | **Preserved, fully live, as the fallback** for repos with no `env_dockerfiles/` entry (nothing deleted or commented — the branch is in `converter.build_task`, gated by `resolve_env_dockerfile`) |

TL's position: the milo-bench-samples corpus Dockerfiles are **not** the shape the
client wants ("false" for Dockerfiles); the client should receive the original
task-folder Dockerfile instead.

## How to revert (two independent switches)

1. **Instant, no code change:** set `ENV_DOCKERFILE_SOURCE=template` in the
   environment of `run_eval.sh` / the converter. Every instance then renders the
   old template (a log line records the override).
2. **Per repo:** delete (or `git rm`) that repo's `env_dockerfiles/<org>_m_<repo>/`
   directory. The converter falls back to the template for that repo with a WARN.

Old behavior is byte-identical to the pre-swap output (regression-pinned by
`tests/test_env_dockerfile_swap.py::test_build_task_falls_back_to_template_render`).

## XTLS PR → Dockerfile map (derived, pending TL confirmation)

Derived 2026-08-11 from the **built ECR images' own `GOLANG_VERSION`** (ground
truth — `derive_env_dockerfile_map.py`; metadata inspection only, no pulls).
All 38 PRs mapped, zero exceptions:

| File | Go | PRs |
|---|---|---|
| `Dockerfile` | 1.17 | 119, 141, 258, 309, 348, 629, 722, 990 |
| `Dockerfile1` | 1.20 | 1035, 1504, 1636, 2227 |
| `Dockerfile2` | 1.24 | 2477, 2758, 2911, 3060, 3308, 3391, 3446, 3453, 3533, 3637, 3813, 3819, 4260, 4497, 4576, 4666, 4945 |
| `Dockerfile3` | 1.26 | 4584, 4981, 5535, 5565, 5693, 5762, 5891, 5948, 5971 |

⚠ Note **pr-4584 → Go 1.26** while its numeric neighbors (4497…4945) use 1.24 —
the eras are NOT contiguous PR ranges. This is why the map is derived from the
images rather than hand-written ranges. (Pinned in
`test_real_resolution_dapr_and_xtls`.)

## Accepted risks (flagged before implementation; TL aware and accepted)

1. **Bundle self-inconsistency.** The bundle's own grading machinery assumes the
   old template's additions: `tests/test.sh` hardcodes
   `/opt/python/bin/python3`; `tests/run_tests.py` imports `multi_swe_bench`;
   `tests/config.json` uses `repo_dir=/workspace/<repo>`; `solution/solve.sh`
   cds there. The input Dockerfiles provide **none** of these — a client that
   builds the shipped Dockerfile cannot run the bundle's grader inside it.
2. **Environment not frozen / not the trajectory's room.** dapr's
   `Dockerfile.base` clones the repo at build-time HEAD and never checks out
   `base_commit` (its `ARG BASE_COMMIT` is unused); the XTLS files clone
   nothing at all. Trajectories were recorded inside the per-PR **ECR** images;
   the shipped Dockerfile no longer describes that environment.
3. **Generic per era.** All bundles of one era carry byte-identical
   Dockerfiles; nothing PR-specific remains in the file (the per-PR ECR
   reference is gone).
4. **Divergence from the milo-bench-samples corpus**, whose bundles all use the
   ECR-template form. TL states the corpus is wrong on this point.
5. **`MSB_REF` V-002 note** in the template ("SHA injected by converter") was
   already dead wiring (`--msb-ref` parsed but unused); moot on the input path,
   still cosmetically wrong on the fallback path.

## Live verification (2026-08-11, real dapr bundles)

- Bundle `edd779ae…` (pr-1351, re-exported through the new converter):
  `environment/Dockerfile` **byte-identical** to `env_dockerfiles/dapr_m_dapr/Dockerfile.base`
  (verified with `cmp`).
- Bundle `6816e922…` (pr-1638, full fresh export): same — byte-identical to the input file.
- Fallback path verified separately: `ENV_DOCKERFILE_SOURCE=template` reproduces the
  template render (regression-pinned in `tests/test_env_dockerfile_swap.py`).

## Operational notes

- Existing bundles (e.g. dapr-1351 `edd779ae…`) are **not regenerated** — the
  swap applies to conversions that run after this change.
- Server-side (EC2/QL) runs pick this up only after `git pull`.
- The rubric/assay pipeline never reads the Dockerfile — scoring is unaffected.
- `env_dockerfiles/README.md` documents the layout + how to add a repo batch.
- Known bundle-vs-template trailing-whitespace nit: the pre-swap dapr bundle
  was rendered while the working tree carried an uncommitted whitespace cleanup
  of the template; content is otherwise identical to a fresh template render.
