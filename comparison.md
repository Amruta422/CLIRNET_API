# Search API bake-off decision memo

## Verdict

**Parallel is recommended when search quality, freshness, and authoritative coverage are the primary requirements. TinyFish is the lower-latency alternative and is preferable when response speed is the dominant constraint.**

Across 36 requests per provider, both providers returned 10 results per request and had no recorded request failures. Parallel had substantially higher measured freshness (68.33% vs 10.83%) and slightly higher authority coverage (50.0% vs 49.17%), while TinyFish was substantially faster (p50 804.37 ms vs 2840.28 ms; p95 2666.79 ms vs 5358.18 ms).

The manual top-10 snippet review found 11 "yes" and 1 "partial" assessment for each provider. The main manual difference was q04: Parallel was assessed "yes" at rank 1, while TinyFish was assessed "partial" at rank 1.

## Evidence

- **Latency:** TinyFish p50 = **804.37 ms**, p95 = **2666.79 ms**; Parallel p50 = **2840.28 ms**, p95 = **5358.18 ms**.
- **Freshness:** Parallel = **68.33%**, compared with TinyFish = **10.83%**.
- **Authority:** Parallel = **50.0%**, compared with TinyFish = **49.17%**. Both providers returned **10.0 results/request** with no recorded failures.
- **Manual snippet sufficiency:** both providers had **11/12 "yes"** and **1/12 "partial"** assessments.
- **Cost:** TinyFish recorded an estimated **$0.00** for 36 attempts; Parallel's estimated cost before its free allowance was **$0.18** for 36 attempts, with the recorded run consuming **0.72%** of the stated monthly free allowance.

## Where the loser wins

TinyFish wins on **latency**. For example, on q04 (`community-acquired pneumonia antibiotic resistance`), the TinyFish run returned 10 results and the measured q04 p50 latency was **728.89 ms**, while the corresponding Parallel q04 p50 was **1943.89 ms**.

TinyFish's q04 result set included a directly relevant American Society for Microbiology result:

https://journals.asm.org/doi/10.1128/spectrum.00792-24

This demonstrates the practical trade-off: TinyFish can return relevant search results considerably faster even when Parallel provides stronger snippet-level coverage for this particular query.

## Recommendation by category

| Category | Recommended provider | Evidence |
|---|---|---|
| clinical | Parallel | q04 received a manual "yes" at rank 1 for Parallel versus "partial" for TinyFish; Parallel q04 authority was 73.33%. |
| india | Parallel | Parallel q05 had 73.33% freshness and q06 had 40.0% freshness, while TinyFish recorded 30.0% and 0.0% respectively. |
| recency | Parallel | Parallel overall freshness was 68.33% versus 10.83% for TinyFish; q07 and q08 also showed substantial dated-result coverage. |
| authority | Parallel | Overall authority was 50.0% for Parallel versus 49.17% for TinyFish; Parallel also reached 73.33% authority on q09 and q10. |
| longtail | Parallel | Parallel q11 measured 43.33% authority and 70.0% freshness, compared with TinyFish's 30.0% authority and 20.0% freshness. |
| latency-sensitive | TinyFish | TinyFish p50 latency was 804.37 ms versus 2840.28 ms for Parallel, with p95 of 2666.79 ms versus 5358.18 ms. |

## Caveats

- One network location and one test day.
- Parallel was run in one fixed mode only.
- This is a 12-query sample with three runs per provider.
- The query set covers four clinical queries, two India-focused queries, two recency queries, two authority queries, and two long-tail queries.
- No human relevance model was used.
- Snippet sufficiency is a manual top-10 review, not an LLM evaluation.
- Freshness is based on dates available in returned results; undated results are not counted as fresh.
- Authority is based on the project's authority classification rather than an independent expert review.
- The measured cost figures depend on the provider pricing/free allowance available for this test.

## Measured overall figures

| Provider | Attempts | p50 latency (ms) | p95 latency (ms) | Results/request | Domain diversity | Freshness | Authority |
|---|---:|---:|---:|---:|---:|---:|---:|
| tinyfish | 36 | 804.37 | 2666.79 | 10.0 | 0.225 | 10.83% | 49.17% |
| parallel | 36 | 2840.28 | 5358.18 | 10.0 | 0.1861 | 68.33% | 50.0% |