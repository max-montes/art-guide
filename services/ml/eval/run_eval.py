"""Eval harness for the art-guide retrieval pipeline.

Iterates services/ml/eval/dataset.jsonl, downloads each test image,
optionally applies a PIL perturbation, POSTs to /v1/identify, records
the result, and computes aggregate metrics:

  - recall@1       top-1 candidate matches expected_artwork_id (in-catalog only)
  - recall@3       expected_artwork_id in top-3 candidates (in-catalog only)
  - status_accuracy returned match.status == expected_status (all cases)
  - latency p50/p95/p99  wall-clock ms per request (download + API)

Exits non-zero when any metric falls below the threshold in thresholds.yaml.

Usage
-----
    # Hit the running local server (default):
    python -m eval.run_eval

    # Custom API URL:
    ART_GUIDE_API_URL=http://localhost:8001 python -m eval.run_eval

    # Skip out-of-catalog cases (faster, no Wikimedia downloads):
    python -m eval.run_eval --in-catalog-only

    # Write report to a specific path:
    python -m eval.run_eval --output-dir services/ml/eval/

    # Add inter-request delay for servers with strict rate limiting:
    python -m eval.run_eval --request-delay-s 32

    # Via CLI:
    art-guide-ml eval
    art-guide-ml eval --in-catalog-only --request-delay-s 0
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("eval")

_HERE = Path(__file__).parent
DATASET_PATH = _HERE / "dataset.jsonl"
THRESHOLDS_PATH = _HERE / "thresholds.yaml"

# Met CDN requires browser-like headers (same rule as ingest path).
_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

# Status ranking: higher index = lower confidence.
_STATUS_RANK = {"exact": 0, "likely": 1, "style_only": 2, "no_match": 3}


# ---------------------------------------------------------------- data classes


@dataclass
class TestCase:
    image_url: str
    expected_artwork_id: str | None
    expected_status: str
    category: str          # exact | perturbed | out_of_catalog
    perturbation: str | None  # None | rotate_5 | crop_10pct | brightness_120 | contrast_80 | color_90
    notes: str


@dataclass
class CaseResult:
    case: TestCase
    skipped: bool = False
    skip_reason: str | None = None
    returned_status: str | None = None
    returned_top_id: str | None = None
    returned_confidence: float | None = None
    returned_candidate_ids: list[str] = field(default_factory=list)
    retrieval_ms: int | None = None
    llm_ms: int | None = None
    total_wall_ms: float | None = None
    recall_at_1: bool | None = None   # None for out_of_catalog
    recall_at_3: bool | None = None   # None for out_of_catalog
    status_match: bool = False
    error: str | None = None


@dataclass
class CategoryMetrics:
    n: int = 0
    recall_at_1: float | None = None
    recall_at_3: float | None = None
    status_accuracy: float = 0.0


@dataclass
class Metrics:
    n_total: int
    n_skipped: int
    n_evaluated: int
    recall_at_1: float          # in-catalog cases only
    recall_at_3: float          # in-catalog cases only
    status_accuracy: float      # all non-skipped cases
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    by_category: dict[str, dict[str, Any]]

    def passes(self, thresholds: dict[str, Any]) -> bool:
        if self.recall_at_1 < thresholds.get("recall_at_1", 0.0):
            return False
        if self.recall_at_3 < thresholds.get("recall_at_3", 0.0):
            return False
        if self.status_accuracy < thresholds.get("status_accuracy", 0.0):
            return False
        if self.latency_p99_ms > thresholds.get("latency_p99_ms", float("inf")):
            return False
        return True

    def failures(self, thresholds: dict[str, Any]) -> list[str]:
        out = []
        if self.recall_at_1 < thresholds.get("recall_at_1", 0.0):
            out.append(
                f"recall@1 {self.recall_at_1:.3f} < threshold {thresholds['recall_at_1']}"
            )
        if self.recall_at_3 < thresholds.get("recall_at_3", 0.0):
            out.append(
                f"recall@3 {self.recall_at_3:.3f} < threshold {thresholds['recall_at_3']}"
            )
        if self.status_accuracy < thresholds.get("status_accuracy", 0.0):
            out.append(
                f"status_accuracy {self.status_accuracy:.3f} < threshold {thresholds['status_accuracy']}"
            )
        if self.latency_p99_ms > thresholds.get("latency_p99_ms", float("inf")):
            out.append(
                f"latency_p99 {self.latency_p99_ms:.0f}ms > threshold {thresholds['latency_p99_ms']}ms"
            )
        return out


# ------------------------------------------------------------------ load helpers


def load_dataset(path: Path = DATASET_PATH) -> list[TestCase]:
    """Parse dataset.jsonl into TestCase objects."""
    cases = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"dataset.jsonl line {lineno}: {exc}") from exc
            cases.append(
                TestCase(
                    image_url=obj["image_url"],
                    expected_artwork_id=obj.get("expected_artwork_id"),
                    expected_status=obj["expected_status"],
                    category=obj.get("category", "exact"),
                    perturbation=obj.get("perturbation"),
                    notes=obj.get("notes", ""),
                )
            )
    return cases


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    """Parse thresholds.yaml. Returns defaults if file is missing."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("PyYAML not installed; using built-in fallback threshold parser.")
        yaml = None  # type: ignore[assignment]

    if not path.exists():
        logger.warning("thresholds.yaml not found at %s; using defaults.", path)
        return {
            "recall_at_1": 0.80,
            "recall_at_3": 0.90,
            "status_accuracy": 0.65,
            "latency_p99_ms": 5000,
        }

    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)

    # Minimal YAML parser for our simple key: value file.
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip()
            try:
                out[k.strip()] = float(v) if "." in v else int(v)
            except ValueError:
                out[k.strip()] = v
    return out


# ------------------------------------------------------------------ image helpers


def download_image(url: str, client: Any) -> bytes:
    """Download image bytes using the provided httpx client.

    Uses browser-like headers for the Met CDN (required to avoid 406 errors).
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    response = client.get(url, headers=_IMAGE_HEADERS, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.content


def apply_perturbation(image_bytes: bytes, perturbation: str | None) -> bytes:
    """Apply a PIL transformation to image bytes and return new JPEG bytes.

    Perturbation types
    ------------------
    rotate_5       Rotate 5° clockwise; fill exposed corners with mid-grey.
    crop_10pct     Crop 10% from each edge (centre-crop); simulates framing.
    brightness_120 Increase brightness by 1.2× (gallery spotlight).
    contrast_80    Reduce contrast to 0.8× (flat phone capture).
    color_90       Reduce colour saturation to 0.9× (slight desaturation).
    """
    if perturbation is None:
        return image_bytes

    from PIL import Image, ImageEnhance  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    if perturbation == "rotate_5":
        img = img.rotate(5, expand=False, fillcolor=(128, 128, 128))
    elif perturbation == "crop_10pct":
        w, h = img.size
        left = int(w * 0.10)
        top = int(h * 0.10)
        right = int(w * 0.90)
        bottom = int(h * 0.90)
        img = img.crop((left, top, right, bottom))
    elif perturbation == "brightness_120":
        img = ImageEnhance.Brightness(img).enhance(1.2)
    elif perturbation == "contrast_80":
        img = ImageEnhance.Contrast(img).enhance(0.8)
    elif perturbation == "color_90":
        img = ImageEnhance.Color(img).enhance(0.9)
    else:
        raise ValueError(f"Unknown perturbation: '{perturbation}'")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ------------------------------------------------------------------ API call


def call_identify(
    image_bytes: bytes,
    api_url: str,
    api_key: str | None,
    client: Any,
    *,
    max_retries: int = 4,
) -> tuple[dict[str, Any], float]:
    """POST image bytes to /v1/identify and return (parsed JSON, net_ms).

    net_ms is the time of the final successful HTTP POST only — it excludes
    client-side retry waits so p99 latency metrics reflect service time, not
    rate-limit backoff delays.

    Retries automatically on 429 (rate limited), honoring the Retry-After
    header so the eval can run unattended against prod without hitting the
    per-key rate cap.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for attempt in range(max_retries + 1):
        files = {"image": ("eval_image.jpg", image_bytes, "image/jpeg")}
        t0 = time.perf_counter()
        response = client.post(
            f"{api_url.rstrip('/')}/v1/identify",
            files=files,
            headers=headers,
            timeout=60,
        )
        net_ms = (time.perf_counter() - t0) * 1000
        if response.status_code == 429 and attempt < max_retries:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(
                "  429 rate limited — waiting %ds before retry %d/%d",
                retry_after,
                attempt + 1,
                max_retries,
            )
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response.json(), net_ms

    response.raise_for_status()  # exhausted retries
    return response.json(), 0.0  # unreachable


# ------------------------------------------------------------------ evaluation


def evaluate_case_response(case: TestCase, response: dict[str, Any]) -> dict[str, Any]:
    """Extract metrics from a single identify response.

    Returns a dict with:
      returned_status, returned_top_id, returned_confidence,
      returned_candidate_ids, retrieval_ms, llm_ms,
      recall_at_1, recall_at_3, status_match
    """
    match = response.get("match", {})
    diagnostics = response.get("diagnostics", {})
    candidates = match.get("candidates", [])

    returned_status = match.get("status")
    returned_confidence = match.get("confidence")
    candidate_ids = [c.get("id") for c in candidates if c.get("id")]
    returned_top_id = candidate_ids[0] if candidate_ids else None
    retrieval_ms = diagnostics.get("retrieval_ms")
    llm_ms = diagnostics.get("llm_ms")

    is_in_catalog = case.category in ("exact", "perturbed")
    recall_at_1: bool | None = None
    recall_at_3: bool | None = None

    if is_in_catalog and case.expected_artwork_id:
        recall_at_1 = returned_top_id == case.expected_artwork_id
        recall_at_3 = case.expected_artwork_id in candidate_ids[:3]

    status_match = returned_status == case.expected_status

    return {
        "returned_status": returned_status,
        "returned_top_id": returned_top_id,
        "returned_confidence": returned_confidence,
        "returned_candidate_ids": candidate_ids,
        "retrieval_ms": retrieval_ms,
        "llm_ms": llm_ms,
        "recall_at_1": recall_at_1,
        "recall_at_3": recall_at_3,
        "status_match": status_match,
    }


# ------------------------------------------------------------------ metric computation


def compute_metrics(results: list[CaseResult]) -> Metrics:
    """Aggregate all CaseResults into a Metrics object."""
    evaluated = [r for r in results if not r.skipped]
    n_total = len(results)
    n_skipped = len(results) - len(evaluated)

    # recall@1 and recall@3: in-catalog cases only
    in_catalog = [r for r in evaluated if r.recall_at_1 is not None]
    recall_at_1 = (
        sum(1 for r in in_catalog if r.recall_at_1) / len(in_catalog)
        if in_catalog else 0.0
    )
    recall_at_3 = (
        sum(1 for r in in_catalog if r.recall_at_3) / len(in_catalog)
        if in_catalog else 0.0
    )

    # status_accuracy: all non-skipped cases
    status_accuracy = (
        sum(1 for r in evaluated if r.status_match) / len(evaluated)
        if evaluated else 0.0
    )

    # Latency percentiles (wall-clock download + API)
    latencies = [r.total_wall_ms for r in evaluated if r.total_wall_ms is not None]
    if latencies:
        latencies_sorted = sorted(latencies)
        p50 = _percentile(latencies_sorted, 50)
        p95 = _percentile(latencies_sorted, 95)
        p99 = _percentile(latencies_sorted, 99)
    else:
        p50 = p95 = p99 = 0.0

    # Per-category breakdown
    by_category: dict[str, dict[str, Any]] = {}
    for cat in ("exact", "perturbed", "out_of_catalog"):
        cat_results = [r for r in evaluated if r.case.category == cat]
        if not cat_results:
            continue
        cat_in_catalog = [r for r in cat_results if r.recall_at_1 is not None]
        entry: dict[str, Any] = {
            "n": len(cat_results),
            "status_accuracy": (
                sum(1 for r in cat_results if r.status_match) / len(cat_results)
            ),
        }
        if cat_in_catalog:
            entry["recall_at_1"] = (
                sum(1 for r in cat_in_catalog if r.recall_at_1) / len(cat_in_catalog)
            )
            entry["recall_at_3"] = (
                sum(1 for r in cat_in_catalog if r.recall_at_3) / len(cat_in_catalog)
            )
        cat_lat = [r.total_wall_ms for r in cat_results if r.total_wall_ms is not None]
        if cat_lat:
            entry["latency_p50_ms"] = _percentile(sorted(cat_lat), 50)
        by_category[cat] = entry

    return Metrics(
        n_total=n_total,
        n_skipped=n_skipped,
        n_evaluated=len(evaluated),
        recall_at_1=recall_at_1,
        recall_at_3=recall_at_3,
        status_accuracy=status_accuracy,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        latency_p99_ms=p99,
        by_category=by_category,
    )


def _percentile(sorted_vals: list[float], pct: int) -> float:
    """Nearest-rank percentile on a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    idx = max(0, int(len(sorted_vals) * pct / 100) - 1)
    return sorted_vals[idx]


# ------------------------------------------------------------------ report writers


def build_report(
    metrics: Metrics,
    results: list[CaseResult],
    thresholds: dict[str, Any],
    *,
    run_timestamp: str,
    api_url: str,
    dataset_path: Path,
) -> dict[str, Any]:
    """Build the machine-readable JSON report dict."""
    failures = metrics.failures(thresholds)
    return {
        "run_timestamp": run_timestamp,
        "api_url": api_url,
        "dataset": str(dataset_path),
        "passed": metrics.passes(thresholds),
        "threshold_failures": failures,
        "metrics": {
            "n_total": metrics.n_total,
            "n_skipped": metrics.n_skipped,
            "n_evaluated": metrics.n_evaluated,
            "recall_at_1": round(metrics.recall_at_1, 4),
            "recall_at_3": round(metrics.recall_at_3, 4),
            "status_accuracy": round(metrics.status_accuracy, 4),
            "latency_p50_ms": round(metrics.latency_p50_ms, 1),
            "latency_p95_ms": round(metrics.latency_p95_ms, 1),
            "latency_p99_ms": round(metrics.latency_p99_ms, 1),
        },
        "thresholds": thresholds,
        "by_category": metrics.by_category,
        "cases": [_result_to_dict(r) for r in results],
    }


def _result_to_dict(r: CaseResult) -> dict[str, Any]:
    return {
        "image_url": r.case.image_url,
        "category": r.case.category,
        "perturbation": r.case.perturbation,
        "expected_artwork_id": r.case.expected_artwork_id,
        "expected_status": r.case.expected_status,
        "skipped": r.skipped,
        "skip_reason": r.skip_reason,
        "returned_status": r.returned_status,
        "returned_top_id": r.returned_top_id,
        "returned_confidence": r.returned_confidence,
        "returned_candidate_ids": r.returned_candidate_ids,
        "recall_at_1": r.recall_at_1,
        "recall_at_3": r.recall_at_3,
        "status_match": r.status_match,
        "retrieval_ms": r.retrieval_ms,
        "llm_ms": r.llm_ms,
        "total_wall_ms": round(r.total_wall_ms, 1) if r.total_wall_ms is not None else None,
        "error": r.error,
        "notes": r.case.notes,
    }


def format_markdown_summary(
    metrics: Metrics,
    thresholds: dict[str, Any],
    *,
    run_timestamp: str,
    api_url: str,
) -> str:
    """Return a human-readable markdown summary of the eval run."""
    passed = metrics.passes(thresholds)
    status_icon = "✅ PASS" if passed else "❌ FAIL"
    failures = metrics.failures(thresholds)

    lines = [
        f"# Art-Guide Retrieval Eval — {run_timestamp}",
        "",
        f"**Result:** {status_icon}",
        f"**API:** {api_url}",
        f"**Cases:** {metrics.n_evaluated} evaluated, {metrics.n_skipped} skipped, {metrics.n_total} total",
        "",
        "## Metrics",
        "",
        "| Metric | Value | Threshold | Status |",
        "| ------ | ----- | --------- | ------ |",
        _thresh_row("recall@1", metrics.recall_at_1, thresholds.get("recall_at_1"), ge=True),
        _thresh_row("recall@3", metrics.recall_at_3, thresholds.get("recall_at_3"), ge=True),
        _thresh_row("status_accuracy", metrics.status_accuracy, thresholds.get("status_accuracy"), ge=True),
        _thresh_row("latency_p50_ms", metrics.latency_p50_ms, None),
        _thresh_row("latency_p95_ms", metrics.latency_p95_ms, None),
        _thresh_row("latency_p99_ms", metrics.latency_p99_ms, thresholds.get("latency_p99_ms"), ge=False),
        "",
    ]

    if failures:
        lines += ["## Threshold failures", ""]
        for f in failures:
            lines.append(f"- {f}")
        lines.append("")

    if metrics.by_category:
        lines += ["## By category", ""]
        for cat, stats in metrics.by_category.items():
            r1 = f"{stats['recall_at_1']:.3f}" if "recall_at_1" in stats else "—"
            r3 = f"{stats['recall_at_3']:.3f}" if "recall_at_3" in stats else "—"
            sa = f"{stats['status_accuracy']:.3f}"
            p50 = f"{stats['latency_p50_ms']:.0f}ms" if "latency_p50_ms" in stats else "—"
            lines.append(f"**{cat}** (n={stats['n']}): recall@1={r1}, recall@3={r3}, status_acc={sa}, p50={p50}")
        lines.append("")

    return "\n".join(lines)


def _thresh_row(name: str, value: float, threshold: Any, ge: bool = True) -> str:
    val_s = f"{value:.3f}" if isinstance(value, float) else str(value)
    thresh_s = str(threshold) if threshold is not None else "—"
    if threshold is None:
        ok = "—"
    elif ge:
        ok = "✅" if value >= threshold else "❌"
    else:
        ok = "✅" if value <= threshold else "❌"
    return f"| {name} | {val_s} | {thresh_s} | {ok} |"


# ------------------------------------------------------------------ main runner


def run_eval(
    *,
    api_url: str = "http://localhost:8000",
    api_key: str | None = None,
    dataset_path: Path = DATASET_PATH,
    thresholds_path: Path = THRESHOLDS_PATH,
    output_dir: Path | None = None,
    in_catalog_only: bool = False,
    request_delay_s: float = 0.0,
    dry_run: bool = False,
) -> tuple[int, Metrics, list[CaseResult]]:
    """Run the full eval suite and return (exit_code, metrics, results).

    Parameters
    ----------
    api_url:
        Base URL of the running art-guide API server.
    api_key:
        Bearer token. If None, auth header is omitted (local dev mode).
    dataset_path:
        Path to dataset.jsonl.
    thresholds_path:
        Path to thresholds.yaml.
    output_dir:
        Directory to write the JSON report and markdown summary.
        Defaults to the directory containing dataset.jsonl.
    in_catalog_only:
        If True, skip out_of_catalog cases.
    request_delay_s:
        Seconds to sleep between requests. Use ≥32 s to stay under the
        default rate limit of 10 req/5 min on a shared server.
    dry_run:
        Log what would be run but make no HTTP requests.
    """
    import httpx  # noqa: PLC0415

    cases = load_dataset(dataset_path)
    thresholds = load_thresholds(thresholds_path)

    if in_catalog_only:
        cases = [c for c in cases if c.category != "out_of_catalog"]
        logger.info("in-catalog-only mode: %d cases", len(cases))

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    results: list[CaseResult] = []

    with httpx.Client() as client:
        for idx, case in enumerate(cases, 1):
            logger.info(
                "[%d/%d] %s | %s | %s",
                idx,
                len(cases),
                case.category,
                case.perturbation or "no-perturb",
                case.image_url[:80],
            )

            result = CaseResult(case=case)

            if dry_run:
                result.skipped = True
                result.skip_reason = "dry-run"
                results.append(result)
                continue

            t_start = time.perf_counter()

            # Download image
            try:
                raw_bytes = download_image(case.image_url, client)
            except Exception as exc:
                result.skipped = True
                result.skip_reason = f"download_failed: {exc}"
                logger.warning("  SKIP download failed: %s", exc)
                results.append(result)
                continue

            # Apply perturbation
            try:
                image_bytes = apply_perturbation(raw_bytes, case.perturbation)
            except Exception as exc:
                result.skipped = True
                result.skip_reason = f"perturbation_failed: {exc}"
                logger.warning("  SKIP perturbation failed: %s", exc)
                results.append(result)
                continue

            # POST to /v1/identify
            try:
                response, net_ms = call_identify(image_bytes, api_url, api_key, client)
            except Exception as exc:
                result.error = str(exc)
                logger.warning("  ERROR identify call: %s", exc)
                results.append(result)
                if request_delay_s > 0:
                    time.sleep(request_delay_s)
                continue

            result.total_wall_ms = net_ms

            # Extract metrics
            try:
                metrics_dict = evaluate_case_response(case, response)
                result.returned_status = metrics_dict["returned_status"]
                result.returned_top_id = metrics_dict["returned_top_id"]
                result.returned_confidence = metrics_dict["returned_confidence"]
                result.returned_candidate_ids = metrics_dict["returned_candidate_ids"]
                result.retrieval_ms = metrics_dict["retrieval_ms"]
                result.llm_ms = metrics_dict["llm_ms"]
                result.recall_at_1 = metrics_dict["recall_at_1"]
                result.recall_at_3 = metrics_dict["recall_at_3"]
                result.status_match = metrics_dict["status_match"]
            except Exception as exc:
                result.error = f"response_parse_error: {exc}"
                logger.warning("  ERROR parsing response: %s", exc)

            r1_s = "✓" if result.recall_at_1 else ("✗" if result.recall_at_1 is False else "—")
            sm_s = "✓" if result.status_match else "✗"
            logger.info(
                "  status=%s conf=%.3f recall@1=%s status_match=%s wall=%.0fms",
                result.returned_status or "?",
                result.returned_confidence or 0.0,
                r1_s,
                sm_s,
                result.total_wall_ms or 0,
            )

            results.append(result)

            if request_delay_s > 0 and idx < len(cases):
                time.sleep(request_delay_s)

    metrics = compute_metrics(results)
    report = build_report(
        metrics,
        results,
        thresholds,
        run_timestamp=run_ts,
        api_url=api_url,
        dataset_path=dataset_path,
    )
    summary_md = format_markdown_summary(
        metrics,
        thresholds,
        run_timestamp=run_ts,
        api_url=api_url,
    )

    out_dir = output_dir or _HERE
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"baseline-{run_ts}.json"
    md_path = out_dir / f"baseline-{run_ts}.md"

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(summary_md, encoding="utf-8")

    print(summary_md)
    logger.info("JSON report: %s", json_path)
    logger.info("Markdown:    %s", md_path)

    passed = metrics.passes(thresholds)
    exit_code = 0 if passed else 1
    return exit_code, metrics, results


# ------------------------------------------------------------------ CLI entrypoint


def main(argv: list[str] | None = None) -> int:
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="art-guide-ml eval",
        description="Run the art-guide retrieval eval suite.",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("ART_GUIDE_API_URL", "http://localhost:8000"),
        help="Base URL of the running API server (default: $ART_GUIDE_API_URL or http://localhost:8000).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ART_GUIDE_API_KEY") or os.environ.get("API_BEARER_TOKEN"),
        help="Bearer token (default: $ART_GUIDE_API_KEY or $API_BEARER_TOKEN).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET_PATH,
        help=f"Path to dataset.jsonl (default: {DATASET_PATH}).",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=THRESHOLDS_PATH,
        help=f"Path to thresholds.yaml (default: {THRESHOLDS_PATH}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write JSON report + markdown (default: same as dataset).",
    )
    parser.add_argument(
        "--in-catalog-only",
        action="store_true",
        help="Skip out_of_catalog cases (no external image downloads beyond Met CDN).",
    )
    parser.add_argument(
        "--request-delay-s",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep between requests. "
            "Use 32+ to stay under the default 10 req/5 min rate limit. "
            "Default: 0 (for local dev with relaxed rate limits)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cases that would run without making HTTP requests.",
    )

    args = parser.parse_args(argv)

    exit_code, metrics, _ = run_eval(
        api_url=args.api_url,
        api_key=args.api_key,
        dataset_path=args.dataset,
        thresholds_path=args.thresholds,
        output_dir=args.output_dir,
        in_catalog_only=args.in_catalog_only,
        request_delay_s=args.request_delay_s,
        dry_run=args.dry_run,
    )
    return exit_code


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
