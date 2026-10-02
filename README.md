# Search API bake-off

This repository runs the fixed CLIRNET workload against TinyFish Search and
Parallel Search, preserves each response payload, normalizes result fields,
and calculates the requested comparison metrics.

## Setup

Use Python 3.10+ and set API keys in your shell; do not place them in a file
that will be submitted.

```powershell
$env:TINYFISH_API_KEY = "..."
$env:PARALLEL_API_KEY = "..."
```

Run each provider from the same machine and network in one sitting:

```powershell
python run_search.py tinyfish --runs 3 --delay-seconds 2.1
python run_search.py parallel --runs 3 --delay-seconds 2.1 --parallel-mode advanced
```

The script is cache-safe: without `--refresh`, a raw payload already present
for a provider/query/run is not requested again. It writes `results/normalized.csv`,
`metrics.json`, `snippet_assessment.csv`, and a decision-memo template.

Review `snippet_assessment.csv` manually using only each provider's top-ten
titles and snippets. Enter `yes`, `partial`, or `no` plus the supporting rank,
then rerun either command (or use `--metrics-only`) to include it in
`metrics.json` and regenerate `comparison.md`. The script never uses an LLM
for that assessment.

```powershell
python run_search.py tinyfish --metrics-only
```

Before packaging, retain only raw JSON files, normalized CSV, metrics JSON,
the completed comparison memo, `queries.json`, and `run_search.py`. Verify
there are no `.env` files, shell histories, API keys, or ZIP archives inside
the submission directory.
