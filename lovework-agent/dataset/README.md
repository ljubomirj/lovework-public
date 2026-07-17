# LoveWork Dataset

Append-only historical ledgers live here.

- `runs.jsonl` — one row per non-dry-run pipeline run.
- `assessments.jsonl` — one row per scored finding, joined by `run_id` and `advert_hash`.
- `outcomes.jsonl` — passive application/Gmail outcome evidence discovered by `history.py`.

These files are produced by normal crawls. Do not hand-edit them; add correction
or reflection events through LoveWork tooling when that exists.
