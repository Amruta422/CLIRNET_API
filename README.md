# Search API bake-off

This repository runs the fixed CLIRNET workload against TinyFish Search and
Parallel Search, preserves each response payload, normalizes result fields,
and calculates the requested comparison metrics. It makes no requests until
you explicitly run a provider command.

## Setup

Use Python 3.10+ and install the one dependency. Set API keys in your shell;
do not place them in a file that will be submitted.

```powershell
python -m pip install -r requirements.txt
$env:TINYFISH_API_KEY = "..."
$env:PARALLEL_API_KEY = "..."
```

Run each provider from the same machine and network in one sitting. Each
command performs three full passes in this order: `q01` through `q12` for run
1, then run 2, then run 3. Existing run slots are immutable: a raw result or
failure record means that slot will never be requested again by this script.

```powershell
python run_search.py tinyfish --runs 3 --delay-seconds 2.1
python run_search.py parallel --runs 3 --delay-seconds 2.1 --parallel-mode advanced
```

The script is cache-safe and deliberately has no refresh/retry flag. It writes
unmodified successful JSON payloads beneath `results/<provider>/`, valid
structured transport-failure records beneath `results/failures/`, append-only
attempt metadata in `results/request_log.jsonl`, `results/normalized.csv`,
`metrics.json`, `results/snippet_assessment.csv`, and a decision-memo template.

Review `results/snippet_assessment.csv` manually using only each provider's top-ten
titles and snippets. Enter `yes`, `partial`, or `no` plus the supporting rank,
then rerun either command (or use `--metrics-only`) to include it in
`metrics.json` and regenerate `comparison.md`. The script never uses an LLM
for that assessment.

```powershell
python run_search.py tinyfish --metrics-only
```

After all 72 requests and the manual assessment, run preflight. It checks the
fixed query set, all 72 expected raw files, CSV schema, populated metrics and
memo, assessment evidence, and accidental secret material.

```powershell
python run_search.py --preflight
python run_search.py --build-zip CLIRNET_API_submission.zip
```

The build command runs preflight first and creates a ZIP containing exactly:
`queries.json`, `run_search.py`, `results/tinyfish/*.json`,
`results/parallel/*.json`, `results/normalized.csv`,
`results/snippet_assessment.csv`, `metrics.json`, `comparison.md`, and
`README.md`. `.env` files and ZIP files are excluded from both Git and the
generated archive. Do not build the archive until `comparison.md` has been
completed from real measurements; the generated memo is intentionally only a
template.
