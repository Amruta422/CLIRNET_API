#!/usr/bin/env python3
"""Run and evaluate the fixed CLIRNET Search API bake-off workload.

Successful API JSON bodies are preserved byte-for-byte in results/<provider>.
Every request attempt is appended to results/request_log.jsonl. A transport
failure has no provider JSON body, so it is represented by valid structured
metadata under results/failures rather than by an invalid empty .json file.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import tldextract

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
PROVIDERS = ("tinyfish", "parallel")
RUNS = (1, 2, 3)
CSV_FIELDS = ["provider", "query_id", "run", "rank", "title", "url", "domain", "snippet", "published_date", "latency_ms", "raw_file"]
AUTHORITY_HOSTS = ("who.int", "nih.gov", "cdc.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov")
PARALLEL_COST_PER_1000 = {"turbo": 1.0, "fast": 1.0, "basic": 5.0, "advanced": 5.0}
# Disabling network suffix-list fetching makes domain calculations repeatable.
EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
SECRET_PATTERN = re.compile(r"(?i)(?:tinyfish|parallel)[_-]?(?:api)?[_-]?key\s*[=:]\s*[^\s\"']+|(?:tf|pk)_[A-Za-z0-9_-]{16,}")


def load_queries():
    with (ROOT / "queries.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def raw_path(provider, query_id, run):
    return RESULTS / provider / f"{query_id}_run{run}.json"


def failure_path(provider, query_id, run):
    return RESULTS / "failures" / provider / f"{query_id}_run{run}.json"


def request_tinyfish(query):
    key = os.environ["TINYFISH_API_KEY"]
    parameters = {"query": query}
    request = Request("https://api.search.tinyfish.ai?" + urlencode(parameters), headers={"X-API-Key": key, "Accept": "application/json"})
    return request, {"method": "GET", "endpoint": "https://api.search.tinyfish.ai", "parameters": parameters, "result_limit": "Provider default; no explicit limit parameter was sent."}


def request_parallel(query, mode):
    key = os.environ["PARALLEL_API_KEY"]
    # The fixed assignment query is used verbatim in both fields; no provider-specific search intent is added.
    parameters = {"objective": query, "search_queries": [query], "mode": mode}
    request = Request("https://api.parallel.ai/v1/search", data=json.dumps(parameters).encode(), method="POST", headers={"x-api-key": key, "Content-Type": "application/json", "Accept": "application/json"})
    return request, {"method": "POST", "endpoint": "https://api.parallel.ai/v1/search", "parameters": parameters, "result_limit": "Provider default; no explicit result-limit parameter was sent."}


def body_result_count(body):
    """Return the count directly from a provider response body, never CSV rows."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    return len(results) if isinstance(results, list) else None


def perform_request(provider, query, parallel_mode):
    started = time.perf_counter()
    try:
        req, details = request_tinyfish(query) if provider == "tinyfish" else request_parallel(query, parallel_mode)
        with urlopen(req, timeout=45) as response:
            body, status = response.read(), response.status
        failure_kind = error = ""
    except HTTPError as exc:
        body, status, failure_kind, error = exc.read(), exc.code, "http_error", str(exc)
    except (URLError, TimeoutError, OSError) as exc:
        body, status, failure_kind, error = b"", None, "transport_error", f"{type(exc).__name__}: {exc}"
    return body, status, round((time.perf_counter() - started) * 1000, 2), failure_kind, error, details


def append_log(record):
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "request_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_attempts():
    path, attempts = RESULTS / "request_log.jsonl", []
    if path.exists():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                attempts.append(json.loads(line))
            except json.JSONDecodeError:
                attempts.append({"_line": line_number, "_invalid_log_line": True})
    return attempts


def attempts_for_slot(attempts, provider, query_id, run):
    return [a for a in attempts if (a.get("provider"), a.get("query_id"), a.get("run")) == (provider, query_id, run)]


def latest_attempt_for_slot(attempts, provider, query_id, run):
    matching = attempts_for_slot(attempts, provider, query_id, run)
    return matching[-1] if matching else {}


def text(value):
    return value if isinstance(value, str) else ""


def host(url):
    return urlparse(url).netloc.lower().split(":")[0] if url else ""


def registrable_domain(domain):
    extracted = EXTRACT(domain.lower())
    return extracted.top_domain_under_public_suffix or domain.lower()


def authority(domain):
    domain = domain.lower()
    return domain.endswith((".gov", ".edu", ".org")) or any(domain == x or domain.endswith("." + x) for x in AUTHORITY_HOSTS)


def normalized_results(provider, payload):
    source = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(source, list):
        return []
    rows = []
    for index, item in enumerate(source, 1):
        if not isinstance(item, dict):
            continue
        if provider == "tinyfish":
            snippet, published, rank = text(item.get("snippet")), item.get("date", ""), item.get("position") or index
        else:
            excerpts = item.get("excerpts", [])
            snippet = "\n".join(x for x in excerpts if isinstance(x, str)) if isinstance(excerpts, list) else text(excerpts)
            published, rank = item.get("publish_date", ""), index
        url = text(item.get("url"))
        rows.append({"rank": rank, "title": text(item.get("title")), "url": url, "domain": host(url) or text(item.get("site_name")), "snippet": snippet, "published_date": published or ""})
    return rows


def parse_payload(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def build_normalized(queries):
    attempts, rows, records = read_attempts(), [], []
    for provider in PROVIDERS:
        for query in queries:
            for run in RUNS:
                path = raw_path(provider, query["id"], run)
                if not path.exists():
                    continue
                payload, attempt = parse_payload(path), latest_attempt_for_slot(attempts, provider, query["id"], run)
                direct_count = body_result_count(path.read_bytes())
                record = {"provider": provider, "query_id": query["id"], "run": run, "raw_file": str(path.relative_to(ROOT)).replace("\\", "/"), "status": attempt.get("status"), "latency_ms": attempt.get("latency_ms"), "timestamp_utc": attempt.get("timestamp_utc"), "result_count_from_raw": direct_count}
                records.append(record)
                if payload is not None:
                    for result in normalized_results(provider, payload):
                        rows.append({"provider": provider, "query_id": query["id"], "run": run, **result, "latency_ms": attempt.get("latency_ms", ""), "raw_file": record["raw_file"], "_run_timestamp": attempt.get("timestamp_utc", "")})
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "normalized.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    return rows, records, attempts


def percentile(values, p):
    if not values: return None
    values = sorted(values); position = (len(values) - 1) * p; lower, upper = math.floor(position), math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 2)


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


def run_date(timestamp):
    try: return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
    except (AttributeError, ValueError): return None


def assessment_path(): return RESULTS / "snippet_assessment.csv"


def load_assessments():
    if not assessment_path().exists(): return {}
    with assessment_path().open(newline="", encoding="utf-8") as handle:
        return {(r["provider"], r["query_id"]): r for r in csv.DictReader(handle)}


def write_assessment_template(queries, rows):
    assessment_path().parent.mkdir(exist_ok=True); existing = load_assessments()
    fields = ["provider", "query_id", "category", "assessment", "supporting_rank", "notes"]
    with assessment_path().open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        present = {(r["provider"], r["query_id"]) for r in rows}
        for provider in PROVIDERS:
            for query in queries:
                key, old = (provider, query["id"]), existing.get((provider, query["id"]), {})
                if key in present:
                    writer.writerow({"provider": provider, "query_id": query["id"], "category": query["category"], "assessment": old.get("assessment", ""), "supporting_rank": old.get("supporting_rank", ""), "notes": old.get("notes", "")})


def calculate_metrics(queries, rows, records, attempts, parallel_mode="advanced"):
    by_pq, by_pqr, records_by_provider = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows: by_pq[(row["provider"], row["query_id"])].append(row); by_pqr[(row["provider"], row["query_id"], row["run"])].append(row)
    for record in records: records_by_provider[record["provider"]].append(record)
    assessments = load_assessments()
    output = {"methodology": {"latency": "Client wall-clock time, including network and response read.", "result_count": "Count read directly from each raw response's results array.", "domain_diversity": "Unique public-suffix-aware registrable domains divided by results.", "overlap": "Jaccard is calculated separately for matching provider runs; per-query aggregate is the arithmetic mean of non-null run Jaccards.", "authority": "Domains ending .gov/.edu/.org or WHO, NIH, CDC, NCBI, PubMed hosts.", "freshness": "Only provider-returned publication dates; age is measured against that request's recorded UTC date.", "snippet_sufficiency": "Manual, top-10 titles/snippets only; assessment, rank, and notes are preserved in results/snippet_assessment.csv and metrics.json.", "cost": "Rates are the assignment-brief published rates; this is an estimate based on recorded attempts."}, "providers": {}, "cross_provider_overlap": {}}
    for provider in PROVIDERS:
        provider_records, p_rows = records_by_provider[provider], [r for r in rows if r["provider"] == provider]
        latencies = [r["latency_ms"] for r in provider_records if isinstance(r.get("latency_ms"), (int, float))]
        statuses = [a.get("status") for a in attempts if a.get("provider") == provider]
        query_metrics, all_ages = {}, []
        for query in queries:
            group = by_pq[(provider, query["id"])]; q_records = [r for r in provider_records if r["query_id"] == query["id"]]
            q_latencies = [r["latency_ms"] for r in q_records if isinstance(r.get("latency_ms"), (int, float))]
            ages = [(run_date(r["_run_timestamp"]) - parse_date(r["published_date"])).days for r in group if parse_date(r["published_date"]) and run_date(r["_run_timestamp"])]
            all_ages.extend(ages); domains = {registrable_domain(r["domain"]) for r in group if r["domain"]}; assessment = assessments.get((provider, query["id"]), {})
            counts = {str(r["run"]): r["result_count_from_raw"] for r in q_records}; valid_counts = [c for c in counts.values() if isinstance(c, int)]
            query_metrics[query["id"]] = {"category": query["category"], "requests_with_raw_response": len(q_records), "latency_ms": {"p50": percentile(q_latencies, .5), "p95": percentile(q_latencies, .95)}, "result_counts_by_run_from_raw": counts, "mean_results_per_run": round(sum(valid_counts) / max(1, len(valid_counts)), 2), "domain_diversity": round(len(domains) / len(group), 4) if group else None, "freshness_pct": round(100 * len(ages) / len(group), 2) if group else None, "dated_age_days": {"count": len(ages), "p50": percentile(ages, .5), "p95": percentile(ages, .95), "min": min(ages) if ages else None, "max": max(ages) if ages else None}, "authority_pct": round(100 * sum(authority(r["domain"]) for r in group) / len(group), 2) if group else None, "snippet_sufficiency": {"assessment": assessment.get("assessment", ""), "supporting_rank": assessment.get("supporting_rank", ""), "notes": assessment.get("notes", "")}}
        total_counts = [r["result_count_from_raw"] for r in provider_records if isinstance(r.get("result_count_from_raw"), int)]
        value = {"request_attempts": sum(a.get("provider") == provider for a in attempts), "raw_responses": len(provider_records), "latency_ms": {"p50": percentile(latencies, .5), "p95": percentile(latencies, .95)}, "mean_results_per_request": round(sum(total_counts) / max(1, len(total_counts)), 2), "domain_diversity": round(len({registrable_domain(r["domain"]) for r in p_rows if r["domain"]}) / len(p_rows), 4) if p_rows else None, "freshness_pct": round(100 * sum(bool(parse_date(r["published_date"])) and bool(run_date(r["_run_timestamp"])) for r in p_rows) / len(p_rows), 2) if p_rows else None, "dated_age_days": {"count": len(all_ages), "p50": percentile(all_ages, .5), "p95": percentile(all_ages, .95), "min": min(all_ages) if all_ages else None, "max": max(all_ages) if all_ages else None}, "authority_pct": round(100 * sum(authority(r["domain"]) for r in p_rows) / len(p_rows), 2) if p_rows else None, "failures": {"non_200": sum(s not in (200, None) for s in statuses), "timeouts_or_network_errors": sum(s is None for s in statuses), "rate_limit_429": sum(s == 429 for s in statuses), "empty_result_sets": sum(r.get("status") == 200 and r.get("result_count_from_raw") == 0 for r in provider_records)}, "request_defaults_and_limits": {"tinyfish": {"default_location_language": "US/en when neither is sent", "default_domain_type": "web", "page": 0, "result_limit": "provider default; actual count is recorded per raw response"} if provider == "tinyfish" else {"mode": parallel_mode, "result_limit": "provider default; actual count is recorded per raw response"}}, "per_query": query_metrics}
        if provider == "tinyfish": value["cost"] = {"mode": "Search API", "published_usd_per_1000_queries": 0.0, "free_allowance_requests": "Search requests free at any wallet balance", "recorded_attempts": value["request_attempts"], "estimated_usd": 0.0}
        else:
            rate, count = PARALLEL_COST_PER_1000[parallel_mode], value["request_attempts"]
            value["cost"] = {"mode": parallel_mode, "published_usd_per_1000_queries": rate, "free_allowance_requests_per_month": 5000, "recorded_attempts": count, "free_allowance_consumed_pct": round(100 * count / 5000, 2), "estimated_usd_before_free_allowance": round(rate * count / 1000, 4), "estimated_usd_after_free_allowance": 0.0 if count <= 5000 else round(rate * (count - 5000) / 1000, 4)}
        output["providers"][provider] = value
    for query in queries:
        per_run, scores = {}, []
        for run in RUNS:
            left, right = {r["url"] for r in by_pqr[("tinyfish", query["id"], run)] if r["url"]}, {r["url"] for r in by_pqr[("parallel", query["id"], run)] if r["url"]}
            score = round(len(left & right) / len(left | right), 4) if left | right else None
            per_run[str(run)] = {"tinyfish_urls": len(left), "parallel_urls": len(right), "shared_urls": len(left & right), "jaccard": score}
            if score is not None: scores.append(score)
        output["cross_provider_overlap"][query["id"]] = {"category": query["category"], "per_run": per_run, "mean_jaccard_across_runs": round(sum(scores) / len(scores), 4) if scores else None}
    return output


def write_memo_template(queries, metrics):
    p = metrics["providers"]
    lines = ["# Search API bake-off decision memo", "", "## Verdict", "", "**Complete after reviewing real metrics from all 72 requests and the manual snippet assessment. Do not infer a winner before then.**", "", "## Evidence", "", "- [Insert metric and value from `metrics.json`.]", "- [Insert metric and value from `metrics.json`.]", "- [Insert metric and value from `metrics.json`.]", "", "## Where the loser wins", "", "- [Name one query, provider, and a result URL that demonstrates the exception.]", "", "## Recommendation by category", "", "| Category | Recommended provider | Evidence |", "|---|---|---|"]
    for category in dict.fromkeys(q["category"] for q in queries): lines.append(f"| {category} | [decide] | [cite metrics/query] |")
    lines += ["", "## Caveats", "", "- One network location and one test day.", "- Parallel was run in one fixed mode only.", "- This is a 12-query sample and includes no human relevance model.", "- Snippet sufficiency is a manual top-10 review, not an LLM evaluation.", "", "## Measured overall figures", "", "| Provider | Attempts | p50 latency (ms) | p95 latency (ms) | Results/request | Domain diversity | Freshness | Authority |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for provider in PROVIDERS:
        v = p[provider]; lines.append(f"| {provider} | {v['request_attempts']} | {v['latency_ms']['p50']} | {v['latency_ms']['p95']} | {v['mean_results_per_request']} | {v['domain_diversity']} | {v['freshness_pct']}% | {v['authority_pct']}% |")
    (ROOT / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def expected_archive_paths(queries):
    paths = {"queries.json", "run_search.py", "metrics.json", "comparison.md", "README.md", "results/normalized.csv", "results/snippet_assessment.csv"}
    for provider in PROVIDERS:
        for query in queries:
            for run in RUNS: paths.add(f"results/{provider}/{query['id']}_run{run}.json")
    return paths


def validate_submission(root=ROOT, archive=None):
    """Return actionable validation errors. This function performs no HTTP calls."""
    errors = []
    try: queries = json.loads((root / "queries.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: return [f"Cannot read queries.json: {exc}"]
    if len(queries) != 12 or [q.get("id") for q in queries] != [f"q{i:02d}" for i in range(1, 13)]: errors.append("queries.json must contain exactly q01 through q12 in order.")
    for provider in PROVIDERS:
        directory = root / "results" / provider; files = list(directory.glob("*.json")) if directory.exists() else []
        if len(files) != 36: errors.append(f"results/{provider} must contain exactly 36 raw JSON files (found {len(files)}).")
        for query in queries:
            for run in RUNS:
                path = directory / f"{query['id']}_run{run}.json"
                if not path.exists(): errors.append(f"Missing raw result: {path.relative_to(root)}")
                elif parse_payload(path) is None: errors.append(f"Raw result is not valid JSON: {path.relative_to(root)}")
    log_path = root / "results" / "request_log.jsonl"
    if not log_path.exists():
        errors.append("Missing results/request_log.jsonl.")
    else:
        try:
            logged_attempts = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except json.JSONDecodeError:
            logged_attempts = []
            errors.append("results/request_log.jsonl contains invalid JSON.")
        for provider in PROVIDERS:
            for query in queries:
                for run in RUNS:
                    count = sum((a.get("provider"), a.get("query_id"), a.get("run")) == (provider, query["id"], run) for a in logged_attempts)
                    if count != 1:
                        errors.append(f"Request log must have exactly one attempt for {provider} {query['id']} run {run} (found {count}).")
    csv_path = root / "results" / "normalized.csv"
    if not csv_path.exists(): errors.append("Missing results/normalized.csv.")
    else:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            if (csv.DictReader(handle).fieldnames or []) != CSV_FIELDS: errors.append("results/normalized.csv does not have the required columns in the required order.")
    assessment = root / "results" / "snippet_assessment.csv"
    if not assessment.exists(): errors.append("Missing results/snippet_assessment.csv.")
    else:
        with assessment.open(newline="", encoding="utf-8") as handle: entries = list(csv.DictReader(handle))
        if len(entries) != 24 or any(x.get("assessment", "").lower() not in {"yes", "partial", "no"} or not x.get("supporting_rank") or not x.get("notes") for x in entries): errors.append("Snippet assessment must contain completed yes/partial/no, supporting rank, and notes for all 24 provider/query pairs.")
    metrics, memo = root / "metrics.json", root / "comparison.md"
    if not metrics.exists(): errors.append("Missing metrics.json.")
    else:
        try:
            data = json.loads(metrics.read_text(encoding="utf-8"))
            if not all(data.get("providers", {}).get(p, {}).get("request_attempts") == 36 for p in PROVIDERS): errors.append("metrics.json is not populated with 36 recorded attempts per provider.")
        except json.JSONDecodeError: errors.append("metrics.json is invalid JSON.")
    if not memo.exists(): errors.append("Missing comparison.md.")
    elif "[Insert metric" in memo.read_text(encoding="utf-8") or "[decide]" in memo.read_text(encoding="utf-8"): errors.append("comparison.md is still a template and must be completed from real results.")
    for path in [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and p.suffix.lower() in {".py", ".json", ".csv", ".md", ".txt", ".env"}]:
        if path.name.startswith(".env"): errors.append(f"Secret-risk file present: {path.relative_to(root)}")
        elif SECRET_PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")): errors.append(f"Possible API key/secret found: {path.relative_to(root)}")
    if archive:
        expected = expected_archive_paths(queries)
        try:
            with zipfile.ZipFile(archive) as zipped:
                names = set(zipped.namelist()); missing, extras = expected - names, names - expected
                if missing: errors.append("ZIP is missing: " + ", ".join(sorted(missing)))
                if extras: errors.append("ZIP contains unexpected files: " + ", ".join(sorted(extras)))
                if any(Path(name).name.startswith(".env") or name.endswith(".zip") for name in names): errors.append("ZIP contains an excluded .env or ZIP file.")
        except (OSError, zipfile.BadZipFile) as exc: errors.append(f"Cannot read ZIP: {exc}")
    return errors


def build_zip(destination, queries):
    errors = validate_submission(ROOT)
    if errors: raise ValueError("Preflight failed:\n- " + "\n- ".join(errors))
    destination = destination.resolve()
    if destination.parent != ROOT: raise ValueError("Write the final ZIP in the project root so its contents can be inspected safely.")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zipped:
        for relative in sorted(expected_archive_paths(queries)): zipped.write(ROOT / relative, relative)
    errors = validate_submission(ROOT, destination)
    if errors: raise ValueError("ZIP verification failed:\n- " + "\n- ".join(errors))


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("provider", nargs="?", choices=PROVIDERS)
    parser.add_argument("--parallel-mode", default="advanced", choices=("advanced", "fast", "turbo", "basic")); parser.add_argument("--delay-seconds", type=float, default=2.1)
    parser.add_argument("--metrics-only", action="store_true"); parser.add_argument("--preflight", action="store_true"); parser.add_argument("--build-zip", type=Path, metavar="PATH")
    args = parser.parse_args()
    if args.preflight:
        errors = validate_submission(ROOT)
        if errors: print("Preflight failed:", *[f"- {x}" for x in errors], sep="\n"); return 1
        print("Preflight passed."); return 0
    if args.build_zip:
        try: build_zip(args.build_zip, load_queries())
        except ValueError as exc: print(exc, file=sys.stderr); return 1
        print(f"Created and verified {args.build_zip}"); return 0
    if not args.provider: parser.error("provide tinyfish or parallel, or use --preflight / --build-zip")
    queries = load_queries()
    if not args.metrics_only:
        attempts = read_attempts()
        # Fixed required order: q01..q12 for run 1, then run 2, then run 3.
        for run in RUNS:
            for index, query in enumerate(queries):
                path, failure = raw_path(args.provider, query["id"], run), failure_path(args.provider, query["id"], run)
                if path.exists() or failure.exists() or attempts_for_slot(attempts, args.provider, query["id"], run): print(f"immutable slot: {args.provider} {query['id']} run {run}"); continue
                body, status, latency, failure_kind, error, details = perform_request(args.provider, query["query"], args.parallel_mode); timestamp = datetime.now(timezone.utc).isoformat(); valid_json = body_result_count(body) is not None
                if body and valid_json:
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(body); artifact = str(path.relative_to(ROOT)).replace("\\", "/")
                else:
                    failure.parent.mkdir(parents=True, exist_ok=True)
                    metadata = {"provider": args.provider, "query_id": query["id"], "run": run, "status": status, "failure_kind": failure_kind or "non_json_or_empty_response", "error": error, "timestamp_utc": timestamp}
                    if body: metadata.update({"response_sha256": hashlib.sha256(body).hexdigest(), "response_body_base64": base64.b64encode(body).decode("ascii")})
                    failure.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8"); artifact = str(failure.relative_to(ROOT)).replace("\\", "/")
                record = {"attempt_id": f"{args.provider}-{query['id']}-run{run}-{timestamp}", "provider": args.provider, "query_id": query["id"], "run": run, "status": status, "latency_ms": latency, "timestamp_utc": timestamp, "failure_kind": failure_kind, "error": error, "artifact": artifact, "result_count_from_raw": body_result_count(body), "request": details, "parallel_mode": args.parallel_mode if args.provider == "parallel" else None}
                append_log(record); attempts.append(record); print(f"{args.provider} run {run} {query['id']}: HTTP {status}, {latency} ms")
                if args.delay_seconds and not (run == 3 and index == len(queries) - 1): time.sleep(args.delay_seconds)
    rows, records, attempts = build_normalized(queries); modes = {a.get("parallel_mode") for a in attempts if a.get("provider") == "parallel" and a.get("parallel_mode")}
    if len(modes) > 1: parser.error("Parallel attempts use more than one mode; do not produce a mixed-mode comparison.")
    write_assessment_template(queries, rows); metrics = calculate_metrics(queries, rows, records, attempts, next(iter(modes), args.parallel_mode))
    (ROOT / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"); write_memo_template(queries, metrics)
    print(f"Wrote {len(rows)} normalized rows, metrics.json, comparison.md, and results/snippet_assessment.csv"); return 0


if __name__ == "__main__": raise SystemExit(main())
