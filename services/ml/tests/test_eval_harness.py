"""Unit tests for the eval harness (services/ml/eval/run_eval.py).

All HTTP calls are mocked — no server or internet access required.
Real-image evaluation lives in dataset.jsonl and is only executed by
the integration harness (art-guide-ml eval).
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eval.run_eval import (
    CaseResult,
    Metrics,
    TestCase,
    apply_perturbation,
    compute_metrics,
    evaluate_case_response,
    format_markdown_summary,
    load_dataset,
    load_thresholds,
)

# ------------------------------------------------------------------ fixtures


@pytest.fixture
def exact_case() -> TestCase:
    return TestCase(
        image_url="https://images.metmuseum.org/CRDImages/ep/original/DP119115.jpg",
        expected_artwork_id="met:435809",
        expected_status="exact",
        category="exact",
        perturbation=None,
        notes="The Harvesters",
    )


@pytest.fixture
def perturbed_case() -> TestCase:
    return TestCase(
        image_url="https://images.metmuseum.org/CRDImages/ep/original/DP119115.jpg",
        expected_artwork_id="met:435809",
        expected_status="exact",
        category="perturbed",
        perturbation="rotate_5",
        notes="The Harvesters — rotated",
    )


@pytest.fixture
def out_of_catalog_style_case() -> TestCase:
    return TestCase(
        image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/Van_Gogh_-_Starry_Night.jpg/1280px.jpg",
        expected_artwork_id=None,
        expected_status="style_only",
        category="out_of_catalog",
        perturbation=None,
        notes="Starry Night — not in catalog",
    )


@pytest.fixture
def out_of_catalog_no_match_case() -> TestCase:
    return TestCase(
        image_url="https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg",
        expected_artwork_id=None,
        expected_status="no_match",
        category="out_of_catalog",
        perturbation=None,
        notes="Cat photo",
    )


def _make_identify_response(
    status: str = "exact",
    confidence: float = 1.0,
    candidates: list[dict] | None = None,
    retrieval_ms: int = 120,
    llm_ms: int = 0,
) -> dict:
    if candidates is None:
        candidates = [
            {
                "id": "met:435809",
                "title": "The Harvesters",
                "artist": "Pieter Bruegel the Elder",
                "score": confidence,
            }
        ]
    return {
        "request_id": "srv_TEST",
        "match": {
            "status": status,
            "confidence": confidence,
            "candidates": candidates,
        },
        "explanation": {
            "text": "test explanation",
            "tone": "museum_guide",
            "length": "short",
            "grounded_fields": ["title"],
            "hedged": False,
        },
        "diagnostics": {
            "retrieval_ms": retrieval_ms,
            "llm_ms": llm_ms,
            "model_versions": {"embedding": "siglip-base-224", "llm": "not-configured"},
        },
    }


# ------------------------------------------------------------------ dataset loading


def test_load_dataset_returns_test_cases():
    cases = load_dataset()
    assert len(cases) >= 40, f"Expected ≥40 cases, got {len(cases)}"
    categories = {c.category for c in cases}
    assert "exact" in categories
    assert "perturbed" in categories
    assert "out_of_catalog" in categories


def test_load_dataset_exact_cases_have_artwork_ids():
    cases = load_dataset()
    exact = [c for c in cases if c.category == "exact"]
    assert all(c.expected_artwork_id is not None for c in exact), (
        "All exact cases must have expected_artwork_id"
    )


def test_load_dataset_out_of_catalog_cases_have_null_ids():
    cases = load_dataset()
    ooc = [c for c in cases if c.category == "out_of_catalog"]
    assert all(c.expected_artwork_id is None for c in ooc), (
        "out_of_catalog cases must not have expected_artwork_id"
    )


def test_load_dataset_perturbed_cases_have_perturbation():
    cases = load_dataset()
    perturbed = [c for c in cases if c.category == "perturbed"]
    assert all(c.perturbation is not None for c in perturbed), (
        "Perturbed cases must have a perturbation field"
    )


# ------------------------------------------------------------------ thresholds


def test_load_thresholds_returns_dict():
    thresholds = load_thresholds()
    assert "recall_at_1" in thresholds
    assert "recall_at_3" in thresholds
    assert "status_accuracy" in thresholds
    assert "latency_p99_ms" in thresholds


def test_load_thresholds_missing_file_returns_defaults(tmp_path):
    missing = tmp_path / "missing.yaml"
    t = load_thresholds(missing)
    assert t["recall_at_1"] == 0.80
    assert t["recall_at_3"] == 0.90


# ------------------------------------------------------------------ evaluate_case_response


def test_evaluate_exact_case_hit(exact_case):
    response = _make_identify_response("exact", 1.0, [
        {"id": "met:435809", "title": "The Harvesters", "score": 1.0}
    ])
    result = evaluate_case_response(exact_case, response)
    assert result["recall_at_1"] is True
    assert result["recall_at_3"] is True
    assert result["status_match"] is True
    assert result["returned_top_id"] == "met:435809"
    assert result["returned_status"] == "exact"


def test_evaluate_exact_case_miss(exact_case):
    response = _make_identify_response("exact", 0.72, [
        {"id": "met:435802", "title": "Portrait of Young Man", "score": 0.72}
    ])
    result = evaluate_case_response(exact_case, response)
    assert result["recall_at_1"] is False
    assert result["recall_at_3"] is False
    assert result["status_match"] is True  # status still matches


def test_evaluate_exact_case_in_top3_not_top1(exact_case):
    response = _make_identify_response("exact", 0.91, [
        {"id": "met:435802", "score": 0.91},
        {"id": "met:435803", "score": 0.88},
        {"id": "met:435809", "score": 0.87},  # expected in position 3
    ])
    result = evaluate_case_response(exact_case, response)
    assert result["recall_at_1"] is False
    assert result["recall_at_3"] is True


def test_evaluate_out_of_catalog_no_recall_fields(out_of_catalog_style_case):
    response = _make_identify_response("style_only", 0.60 * 0.8, [
        {"id": "met:435809", "score": 0.60}
    ])
    result = evaluate_case_response(out_of_catalog_style_case, response)
    assert result["recall_at_1"] is None
    assert result["recall_at_3"] is None
    assert result["status_match"] is True  # style_only == style_only


def test_evaluate_no_match_empty_candidates(out_of_catalog_no_match_case):
    response = _make_identify_response("no_match", 0.4, [])
    result = evaluate_case_response(out_of_catalog_no_match_case, response)
    assert result["recall_at_1"] is None
    assert result["status_match"] is True


# ------------------------------------------------------------------ compute_metrics


def _build_result(case: TestCase, **kwargs) -> CaseResult:
    r = CaseResult(case=case)
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


def test_compute_metrics_perfect_recall(exact_case):
    results = [
        _build_result(
            exact_case,
            recall_at_1=True,
            recall_at_3=True,
            status_match=True,
            total_wall_ms=150.0,
        )
        for _ in range(10)
    ]
    m = compute_metrics(results)
    assert m.recall_at_1 == 1.0
    assert m.recall_at_3 == 1.0
    assert m.status_accuracy == 1.0
    assert m.n_evaluated == 10
    assert m.n_skipped == 0


def test_compute_metrics_partial_recall(exact_case):
    results = [
        _build_result(exact_case, recall_at_1=True, recall_at_3=True, status_match=True, total_wall_ms=100.0),
        _build_result(exact_case, recall_at_1=False, recall_at_3=True, status_match=True, total_wall_ms=200.0),
        _build_result(exact_case, recall_at_1=False, recall_at_3=False, status_match=False, total_wall_ms=300.0),
        _build_result(exact_case, recall_at_1=True, recall_at_3=True, status_match=True, total_wall_ms=400.0),
    ]
    m = compute_metrics(results)
    assert abs(m.recall_at_1 - 0.5) < 1e-9
    assert abs(m.recall_at_3 - 0.75) < 1e-9
    assert abs(m.status_accuracy - 0.75) < 1e-9


def test_compute_metrics_skipped_excluded(exact_case):
    results = [
        _build_result(exact_case, recall_at_1=True, recall_at_3=True, status_match=True, total_wall_ms=100.0),
        _build_result(exact_case, skipped=True, skip_reason="download_failed"),
    ]
    m = compute_metrics(results)
    assert m.n_total == 2
    assert m.n_skipped == 1
    assert m.n_evaluated == 1
    assert m.recall_at_1 == 1.0


def test_compute_metrics_out_of_catalog_not_in_recall(
    exact_case, out_of_catalog_style_case
):
    results = [
        _build_result(exact_case, recall_at_1=True, recall_at_3=True, status_match=True, total_wall_ms=100.0),
        _build_result(exact_case, recall_at_1=False, recall_at_3=True, status_match=True, total_wall_ms=200.0),
        # out-of-catalog: recall fields are None — should not count in recall metrics
        _build_result(out_of_catalog_style_case, recall_at_1=None, recall_at_3=None, status_match=True, total_wall_ms=150.0),
    ]
    m = compute_metrics(results)
    assert abs(m.recall_at_1 - 0.5) < 1e-9   # only 2 in-catalog cases
    assert abs(m.recall_at_3 - 1.0) < 1e-9
    assert abs(m.status_accuracy - 1.0) < 1e-9  # all 3 matched status


def test_compute_metrics_latency_percentiles(exact_case):
    latencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
    results = [
        _build_result(exact_case, recall_at_1=True, recall_at_3=True, status_match=True, total_wall_ms=lat)
        for lat in latencies
    ]
    m = compute_metrics(results)
    assert m.latency_p50_ms > 0
    assert m.latency_p95_ms >= m.latency_p50_ms
    assert m.latency_p99_ms >= m.latency_p95_ms


# ------------------------------------------------------------------ Metrics.passes / failures


def test_metrics_passes():
    m = Metrics(
        n_total=10, n_skipped=0, n_evaluated=10,
        recall_at_1=0.90, recall_at_3=0.95,
        status_accuracy=0.80,
        latency_p50_ms=200.0, latency_p95_ms=400.0, latency_p99_ms=800.0,
        by_category={},
    )
    thresholds = {"recall_at_1": 0.80, "recall_at_3": 0.90, "status_accuracy": 0.65, "latency_p99_ms": 5000}
    assert m.passes(thresholds) is True
    assert m.failures(thresholds) == []


def test_metrics_fails_recall():
    m = Metrics(
        n_total=10, n_skipped=0, n_evaluated=10,
        recall_at_1=0.70, recall_at_3=0.85,
        status_accuracy=0.80,
        latency_p50_ms=200.0, latency_p95_ms=400.0, latency_p99_ms=800.0,
        by_category={},
    )
    thresholds = {"recall_at_1": 0.80, "recall_at_3": 0.90, "status_accuracy": 0.65, "latency_p99_ms": 5000}
    assert m.passes(thresholds) is False
    failures = m.failures(thresholds)
    assert any("recall@1" in f for f in failures)
    assert any("recall@3" in f for f in failures)


def test_metrics_fails_latency():
    m = Metrics(
        n_total=10, n_skipped=0, n_evaluated=10,
        recall_at_1=0.90, recall_at_3=0.95,
        status_accuracy=0.80,
        latency_p50_ms=200.0, latency_p95_ms=4000.0, latency_p99_ms=6000.0,
        by_category={},
    )
    thresholds = {"recall_at_1": 0.80, "recall_at_3": 0.90, "status_accuracy": 0.65, "latency_p99_ms": 5000}
    assert m.passes(thresholds) is False
    assert any("latency_p99" in f for f in m.failures(thresholds))


# ------------------------------------------------------------------ apply_perturbation


def _make_small_jpeg() -> bytes:
    """Create a small valid JPEG in memory for perturbation tests."""
    from PIL import Image as PILImage
    img = PILImage.new("RGB", (64, 64), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_apply_perturbation_none_passthrough():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, None)
    assert result == raw


def test_apply_perturbation_rotate_5():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, "rotate_5")
    assert len(result) > 0
    # Verify it's still a valid JPEG
    from PIL import Image as PILImage
    img = PILImage.open(io.BytesIO(result))
    assert img.mode == "RGB"


def test_apply_perturbation_crop_10pct():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, "crop_10pct")
    from PIL import Image as PILImage
    img = PILImage.open(io.BytesIO(result))
    assert img.size[0] < 64 and img.size[1] < 64


def test_apply_perturbation_brightness_120():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, "brightness_120")
    assert len(result) > 0


def test_apply_perturbation_contrast_80():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, "contrast_80")
    assert len(result) > 0


def test_apply_perturbation_color_90():
    raw = _make_small_jpeg()
    result = apply_perturbation(raw, "color_90")
    assert len(result) > 0


def test_apply_perturbation_unknown_raises():
    raw = _make_small_jpeg()
    with pytest.raises(ValueError, match="Unknown perturbation"):
        apply_perturbation(raw, "flip_horizontal")


# ------------------------------------------------------------------ format_markdown_summary


def test_format_markdown_summary_pass():
    m = Metrics(
        n_total=45, n_skipped=0, n_evaluated=45,
        recall_at_1=0.91, recall_at_3=0.97,
        status_accuracy=0.78,
        latency_p50_ms=180.0, latency_p95_ms=320.0, latency_p99_ms=450.0,
        by_category={},
    )
    thresholds = {"recall_at_1": 0.80, "recall_at_3": 0.90, "status_accuracy": 0.65, "latency_p99_ms": 5000}
    md = format_markdown_summary(m, thresholds, run_timestamp="2026-05-10T23-00-00Z", api_url="http://localhost:8000")
    assert "PASS" in md
    assert "recall@1" in md
    assert "0.910" in md


def test_format_markdown_summary_fail():
    m = Metrics(
        n_total=45, n_skipped=2, n_evaluated=43,
        recall_at_1=0.60, recall_at_3=0.75,
        status_accuracy=0.50,
        latency_p50_ms=1000.0, latency_p95_ms=3000.0, latency_p99_ms=6000.0,
        by_category={},
    )
    thresholds = {"recall_at_1": 0.80, "recall_at_3": 0.90, "status_accuracy": 0.65, "latency_p99_ms": 5000}
    md = format_markdown_summary(m, thresholds, run_timestamp="2026-05-10T23-00-00Z", api_url="http://localhost:8000")
    assert "FAIL" in md
    assert "Threshold failures" in md


# ------------------------------------------------------------------ run_eval (mocked HTTP)


def test_run_eval_dry_run(tmp_path):
    """dry-run mode skips all HTTP; all cases marked as skipped."""
    from eval.run_eval import run_eval

    exit_code, metrics, results = run_eval(
        api_url="http://localhost:9999",
        output_dir=tmp_path,
        dry_run=True,
    )
    assert all(r.skipped for r in results)
    assert all(r.skip_reason == "dry-run" for r in results)
    # dry-run has 0 evaluated → recall is 0 → below threshold → exit_code may be 1
    # Just assert it ran without exception:
    assert exit_code in (0, 1)


def test_run_eval_mocked_perfect(tmp_path, exact_case):
    """All in-catalog cases return exact match; verify exit code 0."""
    from eval.run_eval import run_eval

    all_cases_response = _make_identify_response("exact", 1.0, [
        {"id": "met:435809", "title": "The Harvesters", "score": 1.0}
    ])

    mock_image_bytes = _make_small_jpeg()

    with patch("eval.run_eval.download_image", return_value=mock_image_bytes), \
         patch("eval.run_eval.call_identify", return_value=all_cases_response):

        # Use a tiny single-case dataset to keep test fast
        import json
        mini_dataset = tmp_path / "mini.jsonl"
        mini_dataset.write_text(
            json.dumps({
                "image_url": "https://example.com/img.jpg",
                "expected_artwork_id": "met:435809",
                "expected_status": "exact",
                "category": "exact",
                "perturbation": None,
                "notes": "test case",
            }) + "\n"
        )

        # Use permissive thresholds
        import yaml
        thresh_path = tmp_path / "thresholds.yaml"
        thresh_path.write_text("recall_at_1: 0.5\nrecall_at_3: 0.5\nstatus_accuracy: 0.5\nlatency_p99_ms: 9999\n")

        exit_code, metrics, results = run_eval(
            api_url="http://localhost:8000",
            dataset_path=mini_dataset,
            thresholds_path=thresh_path,
            output_dir=tmp_path,
        )

    assert exit_code == 0
    assert metrics.recall_at_1 == 1.0
    assert results[0].status_match is True
    # Verify the JSON report was written
    reports = list(tmp_path.glob("baseline-*.json"))
    assert len(reports) == 1


def test_run_eval_download_failure_skips_case(tmp_path):
    """Cases whose image URL is unreachable are skipped with a clear reason."""
    from eval.run_eval import run_eval
    import json

    mini_dataset = tmp_path / "mini.jsonl"
    mini_dataset.write_text(
        json.dumps({
            "image_url": "https://example.com/404.jpg",
            "expected_artwork_id": "met:435809",
            "expected_status": "exact",
            "category": "exact",
            "perturbation": None,
            "notes": "unreachable image",
        }) + "\n"
    )

    thresh_path = tmp_path / "thresholds.yaml"
    thresh_path.write_text("recall_at_1: 0.0\nrecall_at_3: 0.0\nstatus_accuracy: 0.0\nlatency_p99_ms: 99999\n")

    with patch("eval.run_eval.download_image", side_effect=Exception("connection refused")):
        exit_code, metrics, results = run_eval(
            api_url="http://localhost:8000",
            dataset_path=mini_dataset,
            thresholds_path=thresh_path,
            output_dir=tmp_path,
        )

    assert results[0].skipped is True
    assert "download_failed" in (results[0].skip_reason or "")
    assert metrics.n_skipped == 1
