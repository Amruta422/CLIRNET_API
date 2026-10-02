#!/usr/bin/env python3
"""Run and evaluate the fixed Search API bake-off workload.

Raw API response bodies are written unchanged to results/<provider>/*.json.
Request metadata is kept separately in results/request_log.jsonl so latency is
not mixed into provider payloads.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
PROVIDERS = ("tinyfish", "parallel")
CSV_FIELDS = ["provider", "query_id", "run", "rank", "title", "url", "domain", "snippet", "published_date", "latency_ms", "raw_file"]
AUTHORITY_HOSTS = ("who.int", "nih.gov", "cdc.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov")
PARALLEL_COST_PER_1000 = {"turbo": 1.0, "fast": 1.0, "basic": 5.0, "advanced": 5.0}


def load_queries():
    with (ROOT / "queries.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def raw_path(provider, query_id, run):
    return RESULTS / provider / f"{query_id}_run{run}.json"


def request_tinyfish(query):
    key = os.environ["TINYFISH_API_KEY"]
    url = "https://api.search.tinyfish.ai?" + urlencode({"query": query})
    return Request(url, headers={"X-API-Key": key, "Accept": "application/json"})


def request_parallel(query, mode):
    key = os.environ["PARALLEL_API_KEY"]
    # The API requires an objective. Keeping both fields equal to the fixed
    # workload query avoids adding provider-specific search intent.
    body = json.dumps({"objective": query, "search_queries": [query], "mode": mode}).encode()
    return Request("https://api.parallel.ai/v1/search", data=body, method="POST",
                   headers={"x-api-key": key, "Content-Type": "application/json", "Accept": "application/json"})


def perform_request(provider, query, parallel_mode):
    started = time.perf_counter()
    try:
        req = request_tinyfish(query) if provider == "tinyfish" else request_parallel(query, parallel_mode)
        with urlopen(req, timeout=45) as response:
            payload = response.read()
            status = response.status
        error = ""
    except HTTPError as exc:
        payload, status, error = exc.read(), exc.code, str(exc)
    except (URLError, TimeoutError, OSError) as exc:
        payload, status, error = b"", None, f"{type(exc).__name__}: {exc}"
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    return payload, status, elapsed, error


def append_log(record):
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "request_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_logs():
    path = RESULTS / "request_log.jsonl"
    logs = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                logs[(item["provider"], item["query_id"], item["run"])] = item
            except (json.JSONDecodeError, KeyError):
                continue
    return logs


def text(value):
    return value if isinstance(value, str) else ""


def host(url):
    return urlparse(url).netloc.lower().split(":")[0] if url else ""


def normalized_results(provider, payload):
    source = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(source, list):
        return []
    rows = []
    for index, item in enumerate(source, 1):
        if not isinstance(item, dict):
            continue
        if provider == "tinyfish":
            snippet, published = text(item.get("snippet")), item.get("date", "")
            rank = item.get("position") or index
        else:
            excerpts = item.get("excerpts", [])
            snippet = "\n".join(x for x in excerpts if isinstance(x, str)) if isinstance(excerpts, list) else text(excerpts)
            published, rank = item.get("publish_date", ""), index
        url = text(item.get("url"))
        rows.append({"rank": rank, "title": text(item.get("title")), "url": url,
                     "domain": host(url) or text(item.get("site_name")), "snippet": snippet,
                     "published_date": published or ""})
    return rows


def build_normalized(queries):
    logs, rows = read_logs(), []
    for provider in PROVIDERS:
        for query in queries:
            for run in range(1, 4):
                path = raw_path(provider, query["id"], run)
                if not path.exists():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                log = logs.get((provider, query["id"], run), {})
                for result in normalized_results(provider, payload):
                    rows.append({"provider": provider, "query_id": query["id"], "run": run,
                                 **result, "latency_ms": log.get("latency_ms", ""),
                                 "raw_file": str(path.relative_to(ROOT)).replace("\\", "/")})
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "normalized.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader(); writer.writerows(rows)
    return rows, logs


def percentile(values, p):
    if not values: return None
    values = sorted(values)
    position = (len(values) - 1) * p
    lower, upper = math.floor(position), math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 2)


def registrable_domain(domain):
    parts = domain.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()


def authority(domain):
    domain = domain.lower()
    return domain.endswith((".gov", ".edu", ".org")) or any(domain == x or domain.endswith("." + x) for x in AUTHORITY_HOSTS)


def parse_date(value):
    if not value or not isinstance(value, str): return None
    value = value.strip().replace("Z", "+00:00")
    for candidate in (value, value[:10]):
        try: return datetime.fromisoformat(candidate).date()
        except ValueError: pass
    for fmt in ("%Y/%m/%d", "%d %b %Y", "%b %d, %Y"):
        try: return datetime.strptime(value, fmt).date()
        except ValueError: pass
    return None


def load_assessments():
    path = ROOT / "snippet_assessment.csv"
    if not path.exists(): return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {(r["provider"], r["query_id"]): r for r in csv.DictReader(handle)}


def write_assessment_template(queries, rows):
    path = ROOT / "snippet_assessment.csv"
    existing = load_assessments()
    fields = ["provider", "query_id", "category", "assessment", "supporting_rank", "notes"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        present = {(r["provider"], r["query_id"]) for r in rows}
        for provider in PROVIDERS:
            for query in queries:
                key = (provider, query["id"])
                if key in present:
                    old = existing.get(key, {})
                    writer.writerow({"provider": provider, "query_id": query["id"], "category": query["category"],
                                     "assessment": old.get("assessment", ""), "supporting_rank": old.get("supporting_rank", ""),
                                     "notes": old.get("notes", "")})


def calculate_metrics(queries, rows, logs, parallel_mode="advanced"):
    by_pq = defaultdict(list)
    for row in rows: by_pq[(row["provider"], row["query_id"])].append(row)
    query_map = {q["id"]: q for q in queries}
    assessments = load_assessments()
    output = {"methodology": {"latency": "Client wall-clock time, including network and response read.",
              "domain_diversity": "Unique registrable domains divided by results.",
              "overlap": "Jaccard similarity of unique URLs pooled across three runs for one query.",
              "authority": "Domains ending .gov/.edu/.org or WHO, NIH, CDC, NCBI, PubMed hosts.",
              "freshness": "Only provider-returned publication dates; age is relative to run date.",
              "snippet_sufficiency": "Manual, top-10 titles/snippets only; blank means not reviewed.",
              "cost": "Rates are the assignment-brief published rates; this is an estimate based on logged requests."}, "providers": {}, "cross_provider_overlap": {}}
    for provider in PROVIDERS:
        p_rows = [r for r in rows if r["provider"] == provider]
        latencies = [x["latency_ms"] for k, x in logs.items() if k[0] == provider and isinstance(x.get("latency_ms"), (int, float))]
        statuses = [x.get("status") for k, x in logs.items() if k[0] == provider]
        query_metrics, dated_ages = {}, []
        for query in queries:
            group = by_pq[(provider, query["id"])]
            domains = {registrable_domain(r["domain"]) for r in group if r["domain"]}
            dated = [parse_date(r["published_date"]) for r in group]
            dated = [x for x in dated if x]
            ages = [(date.today() - x).days for x in dated]
            dated_ages.extend(ages)
            q_latencies = [x["latency_ms"] for k, x in logs.items() if k[:2] == (provider, query["id"]) and isinstance(x.get("latency_ms"), (int, float))]
            assessment = assessments.get((provider, query["id"]), {})
            query_metrics[query["id"]] = {"category": query["category"], "requests_logged": len(q_latencies),
                "latency_ms": {"p50": percentile(q_latencies, .50), "p95": percentile(q_latencies, .95)},
                "result_count": len(group), "mean_results_per_run": round(len(group) / max(1, len(q_latencies)), 2),
                "domain_diversity": round(len(domains) / len(group), 4) if group else None,
                "dated_results": len(dated), "freshness_pct": round(100 * len(dated) / len(group), 2) if group else None,
                "authority_pct": round(100 * sum(authority(r["domain"]) for r in group) / len(group), 2) if group else None,
                "snippet_sufficiency": {"assessment": assessment.get("assessment", ""), "supporting_rank": assessment.get("supporting_rank", "")}}
        output["providers"][provider] = {"requests_logged": len(latencies), "latency_ms": {"p50": percentile(latencies, .50), "p95": percentile(latencies, .95)},
            "results": len(p_rows), "mean_results_per_request": round(len(p_rows) / max(1, len(latencies)), 2),
            "domain_diversity": round(len({registrable_domain(r["domain"]) for r in p_rows if r["domain"]}) / len(p_rows), 4) if p_rows else None,
            "freshness_pct": round(100 * sum(bool(parse_date(r["published_date"])) for r in p_rows) / len(p_rows), 2) if p_rows else None,
            "dated_age_days": {"count": len(dated_ages), "p50": percentile(dated_ages, .50), "p95": percentile(dated_ages, .95), "min": min(dated_ages) if dated_ages else None, "max": max(dated_ages) if dated_ages else None},
            "authority_pct": round(100 * sum(authority(r["domain"]) for r in p_rows) / len(p_rows), 2) if p_rows else None,
            "failures": {"non_200": sum(x not in (200, None) for x in statuses), "timeouts_or_network_errors": sum(x is None for x in statuses), "rate_limit_429": sum(x == 429 for x in statuses), "empty_result_sets": sum(not by_pq[(provider, q["id"])] for q in queries)},
            "per_query": query_metrics}
        if provider == "tinyfish":
            output["providers"][provider]["cost"] = {"mode": "Search API", "published_usd_per_1000_queries": 0.0, "free_allowance_requests": "Search requests free at any wallet balance", "logged_requests": len(latencies), "estimated_usd": 0.0}
        else:
            per_1000 = PARALLEL_COST_PER_1000[parallel_mode]
            output["providers"][provider]["cost"] = {"mode": parallel_mode, "published_usd_per_1000_queries": per_1000, "free_allowance_requests_per_month": 5000, "logged_requests": len(latencies), "free_allowance_consumed_pct": round(100 * len(latencies) / 5000, 2), "estimated_usd_before_free_allowance": round(per_1000 * len(latencies) / 1000, 4), "estimated_usd_after_free_allowance": 0.0 if len(latencies) <= 5000 else round(per_1000 * (len(latencies) - 5000) / 1000, 4)}
    for query in queries:
        left = {r["url"] for r in by_pq[("tinyfish", query["id"])] if r["url"]}
        right = {r["url"] for r in by_pq[("parallel", query["id"])] if r["url"]}
        output["cross_provider_overlap"][query["id"]] = {"category": query["category"], "tinyfish_urls": len(left), "parallel_urls": len(right), "shared_urls": len(left & right), "jaccard": round(len(left & right) / len(left | right), 4) if left | right else None}
    return output


def write_memo_template(queries, metrics):
    p = metrics["providers"]
    lines = ["# Search API bake-off decision memo", "", "## Verdict", "", "**Complete after reviewing the measured metrics; do not infer a winner before both providers have 36 successful requests.**", "", "## Evidence", "", "- [Insert metric and value from `metrics.json`.]", "- [Insert metric and value from `metrics.json`.]", "- [Insert metric and value from `metrics.json`.]", "", "## Where the loser wins", "", "- [Name one query, provider, and a result URL that demonstrates the exception.]", "", "## Recommendation by category", "", "| Category | Recommended provider | Evidence |", "|---|---|---|"]
    for category in dict.fromkeys(q["category"] for q in queries): lines.append(f"| {category} | [decide] | [cite metrics/query] |")
    lines += ["", "## Caveats", "", "- One network location and one test day.", "- Parallel was run in one fixed mode only.", "- This is a 12-query sample and includes no human relevance model.", "- Snippet sufficiency is a manual top-10 review, not an LLM evaluation.", "", "## Measured overall figures", "", "| Provider | Requests | p50 latency (ms) | p95 latency (ms) | Results/request | Domain diversity | Freshness | Authority |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for provider in PROVIDERS:
        v = p[provider]
        lines.append(f"| {provider} | {v['requests_logged']} | {v['latency_ms']['p50']} | {v['latency_ms']['p95']} | {v['mean_results_per_request']} | {v['domain_diversity']} | {v['freshness_pct']}% | {v['authority_pct']}% |")
    (ROOT / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--runs", type=int, default=3, help="Must remain 3 for the assignment.")
    parser.add_argument("--parallel-mode", default="advanced", choices=("advanced", "fast", "turbo", "basic"))
    parser.add_argument("--delay-seconds", type=float, default=2.1)
    parser.add_argument("--refresh", action="store_true", help="Deliberately re-request already cached runs.")
    parser.add_argument("--metrics-only", action="store_true")
    args = parser.parse_args()
    if args.runs != 3: parser.error("The assignment requires exactly three runs per query.")
    queries = load_queries()
    if not args.metrics_only:
        for query_index, query in enumerate(queries):
            for run in range(1, args.runs + 1):
                path = raw_path(args.provider, query["id"], run)
                if path.exists() and not args.refresh:
                    print(f"cached: {path.relative_to(ROOT)}")
                    continue
                payload, status, latency_ms, error = perform_request(args.provider, query["query"], args.parallel_mode)
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
                append_log({"provider": args.provider, "query_id": query["id"], "run": run, "status": status,
                            "latency_ms": latency_ms, "error": error, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                            "raw_file": str(path.relative_to(ROOT)).replace("\\", "/"), "parallel_mode": args.parallel_mode if args.provider == "parallel" else None})
                print(f"{args.provider} {query['id']} run {run}: HTTP {status}, {latency_ms} ms")
                if args.delay_seconds and not (query_index == len(queries) - 1 and run == args.runs): time.sleep(args.delay_seconds)
    rows, logs = build_normalized(queries)
    observed_parallel_modes = {item.get("parallel_mode") for key, item in logs.items()
                               if key[0] == "parallel" and item.get("parallel_mode")}
    if len(observed_parallel_modes) > 1:
        parser.error("Parallel runs use more than one mode; start over with one fixed mode for a valid comparison.")
    report_parallel_mode = next(iter(observed_parallel_modes), args.parallel_mode)
    write_assessment_template(queries, rows)
    metrics = calculate_metrics(queries, rows, logs, report_parallel_mode)
    (ROOT / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_memo_template(queries, metrics)
    print(f"Wrote {len(rows)} normalized rows, metrics.json, comparison.md, and snippet_assessment.csv")


if __name__ == "__main__":
    main()
