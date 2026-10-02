# Search API bake-off decision memo

## Verdict

**Complete after reviewing real metrics from all 72 requests and the manual snippet assessment. Do not infer a winner before then.**

## Evidence

- [Insert metric and value from `metrics.json`.]
- [Insert metric and value from `metrics.json`.]
- [Insert metric and value from `metrics.json`.]

## Where the loser wins

- [Name one query, provider, and a result URL that demonstrates the exception.]

## Recommendation by category

| Category | Recommended provider | Evidence |
|---|---|---|
| clinical | [decide] | [cite metrics/query] |
| india | [decide] | [cite metrics/query] |
| recency | [decide] | [cite metrics/query] |
| authority | [decide] | [cite metrics/query] |
| longtail | [decide] | [cite metrics/query] |

## Caveats

- One network location and one test day.
- Parallel was run in one fixed mode only.
- This is a 12-query sample and includes no human relevance model.
- Snippet sufficiency is a manual top-10 review, not an LLM evaluation.

## Measured overall figures

| Provider | Attempts | p50 latency (ms) | p95 latency (ms) | Results/request | Domain diversity | Freshness | Authority |
|---|---:|---:|---:|---:|---:|---:|---:|
| tinyfish | 0 | None | None | 0.0 | None | None% | None% |
| parallel | 0 | None | None | 0.0 | None | None% | None% |
