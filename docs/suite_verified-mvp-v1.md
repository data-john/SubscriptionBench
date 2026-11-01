# Suite `verified-mvp-v1`

Frozen 2026-10-31. Never edit in place: a changed suite becomes `v2`, and longitudinal
comparisons only span runs on the same suite version.

| Item | Value |
|---|---|
| Source | `princeton-nlp/SWE-bench_Verified`, revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` (parquet, 500 rows) |
| Selection | seed `20261031`, 4 tasks per difficulty stratum (`<15 min fix`, `15 min - 1 hour`, `1-4 hours`), 12 tasks |
| Repos | pytest (3), requests (3), sphinx (3), sympy (2), django (1), astropy (1). Candidate pool restricted to these 6 repos |
| Order | immutable stratified round-robin: easy, medium, hard, easy, ... (`order` field). A truncated run stays balanced; the 3-task pilot covers one task per stratum |
| Grader | `swebench==4.1.0` (the last 4.x release; 5.x requires an `image` column the pinned dataset lacks) with prebuilt `swebench/sweb.eval.x86_64.*` images |
| Rejected candidates | none (`verified-mvp-v1.rejected.json` is empty) |

Files: [`suites/verified-mvp-v1.json`](../suites/verified-mvp-v1.json) (suite),
[`.validation.json`](../suites/verified-mvp-v1.validation.json) (control results),
[`.images.json`](../suites/verified-mvp-v1.images.json) (image ids and sizes),
[`.rejected.json`](../suites/verified-mvp-v1.rejected.json).

## Validation

Every task was graded in **2 independent harness runs** per control:

* **Gold patch** resolved the task: 12/12 tasks, both runs.
* **No-op patch** (an empty new file; applies cleanly, fixes nothing) did **not** resolve: 12/12 tasks, both runs. This is a stronger control than an empty patch, which the harness skips without running tests.

The 12 candidates were the first picks of the seeded ranking, so no replacements were needed.

## Disk footprint

* The 12 instance images are 2.67 to 3.01 GB each (33.4 GB if counted separately).
* They share layers: free space on `/` dropped by about **13 GB** after pulling them.
* The harness deletes pulled instance images after grading (default `cache_level=env`), so `scripts/pull_images.py` pre-pulls and keeps them. Re-run it after any `docker system prune`.

## Reproduce

```bash
uv run --group suite subbench suite build --revision c104f840cc67f8b6eec6f759ebc8b2693d585d4a \
    --version verified-mvp-v1 --out suites/verified-mvp-v1.json \
    --rejected-file suites/verified-mvp-v1.rejected.json
uv run --group suite subbench suite validate suites/verified-mvp-v1.json \
    --report suites/verified-mvp-v1.validation.json
python3 scripts/pull_images.py suites/verified-mvp-v1.json --out suites/verified-mvp-v1.images.json
```
