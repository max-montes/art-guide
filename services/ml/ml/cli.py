"""Command-line entry point for the art-guide ML service.

Usage examples
--------------
    art-guide-ml ingest met --limit 100
    art-guide-ml ingest met --limit 5 --dry-run
    art-guide-ml ingest met --limit 200 --jsonl-out data/met/normalized.jsonl
    art-guide-ml augment --source path/to/img.jpg --artwork-id met:436532 \\
        --out evals/datasets/augmented/ --count 10
    art-guide-ml embed path/to/img.jpg
    art-guide-ml eval
    art-guide-ml eval --in-catalog-only
    art-guide-ml eval --request-delay-s 32
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from tqdm import tqdm

from ml.ingest.met import MetIngestionAdapter
from ml.schema import NormalizedArtwork

logger = logging.getLogger("ml.cli")


# --------------------------------------------------------------------- ingest

DEFAULT_LOCAL_DSN = "postgresql://art_guide:art_guide@localhost:5432/art_guide"


def _cmd_ingest_met(args: argparse.Namespace) -> int:
    """Fetch Met records, embed, and UPSERT into Postgres.

    Default behavior (and the one wired into the retrieval critical path):
    DB ingest. ``--jsonl-out PATH`` keeps the legacy normalize-only flow
    available for offline data exploration; ``--dry-run`` runs the whole
    fetch→embed pipeline without touching the DB.
    """
    if args.jsonl_out:
        return _ingest_met_jsonl(args)
    return asyncio.run(_ingest_met_to_db(args))


def _ingest_met_jsonl(args: argparse.Namespace) -> int:
    """Legacy path: write normalized records to JSONL, no embedding, no DB."""
    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    adapter = MetIngestionAdapter(limit=args.limit)
    written = 0
    try:
        with out_path.open("w", encoding="utf-8") as fh:
            iterator = adapter.iter_records()
            progress = tqdm(
                iterator,
                total=args.limit if args.limit is not None else None,
                desc="met-jsonl",
                unit="rec",
            )
            for record in progress:
                if not isinstance(record, NormalizedArtwork):
                    logger.warning("Skipping non-NormalizedArtwork: %r", record)
                    continue
                fh.write(record.to_jsonl())
                fh.write("\n")
                written += 1
    finally:
        adapter.close()

    print(f"Wrote {written} records to {out_path}")
    return 0


async def _ingest_met_to_db(args: argparse.Namespace) -> int:
    """DB path: fetch → preprocess → embed (SigLIP D=768) → UPSERT."""
    # Local imports so JSONL path / unrelated subcommands don't pay torch + asyncpg cost.
    import asyncpg

    from ml.ingest.met_db import (
        DEFAULT_BATCH_COMMIT_SIZE,
        ingest_met_to_db,
    )

    limit = args.limit if args.limit is not None else 100
    batch_size = args.batch_commit_size or DEFAULT_BATCH_COMMIT_SIZE
    department_ids = (
        [int(x) for x in args.department_ids.split(",") if x.strip()]
        if args.department_ids
        else None
    )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not open Postgres pool ({exc!s}). "
                "Pass --dry-run to skip DB writes, or bring up "
                "infra/docker-compose.yml first.",
                file=sys.stderr,
            )
            return 2

    try:
        stats = await ingest_met_to_db(
            pool=pool,
            limit=limit,
            batch_commit_size=batch_size,
            department_ids=department_ids,
            dry_run=args.dry_run,
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    print(
        f"[{mode}] Met ingest done: "
        f"candidates={summary['candidate_ids']}, "
        f"fetched={summary['fetched']}, "
        f"persisted={summary['total_persisted']} "
        f"(inserted={summary['inserted']}, updated={summary['updated']}), "
        f"skipped_filter={summary['skipped_filter']}, "
        f"skipped_image={summary['skipped_image_error']}, "
        f"skipped_embed={summary['skipped_embed_error']}, "
        f"skipped_db={summary['skipped_db_error']}, "
        f"rate_limited={summary['rate_limited_events']}"
    )
    return 0


def _scrub_dsn(dsn: str) -> str:
    """Redact the password segment of a Postgres DSN for log lines."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


# -------------------------------------------------------------------- augment

def _cmd_augment(args: argparse.Namespace) -> int:
    # Local import so `python -m ml.cli` doesn't require Pillow until needed.
    from ml.eval_data.augment import augment_image

    manifest_path = augment_image(
        source_path=args.source,
        out_dir=args.out,
        artwork_id=args.artwork_id,
        count=args.count,
    )
    print(f"Wrote {args.count} variants. Manifest: {manifest_path}")
    return 0


# ---------------------------------------------------------------------- embed

def _cmd_embed(args: argparse.Namespace) -> int:
    """Embed a single image with the chosen embedder; print shape and head.

    Proves the wrapper end-to-end: model loads, preprocessing routes
    through the shared image pipeline, and the result is L2-normalized.
    """
    # Local imports so unrelated CLI usage doesn't pull torch.
    from ml.embeddings import DEFAULT_EMBEDDER_NAME, get_embedder

    src = Path(args.image)
    if not src.exists():
        print(f"image not found: {src}", file=sys.stderr)
        return 2

    model_name = args.model or DEFAULT_EMBEDDER_NAME
    embedder = get_embedder(model_name)
    vec = embedder.embed_path(src)

    head = ", ".join(f"{v: .4f}" for v in vec[: args.head].tolist())
    norm = float((vec * vec).sum() ** 0.5)
    print(f"model:  {model_name}  (D={embedder.spec.dim})")
    print(f"shape:  {vec.shape}  dtype={vec.dtype}")
    print(f"L2:     {norm:.6f}")
    print(f"first {args.head}: [{head}]")
    return 0


# --------------------------------------------------------------------- eval

def _cmd_eval(args: argparse.Namespace) -> int:
    """Run the retrieval eval harness against a live API server."""
    import os
    from pathlib import Path
    from eval.run_eval import DATASET_PATH, THRESHOLDS_PATH, run_eval  # noqa: PLC0415

    api_url = args.api_url or os.environ.get("ART_GUIDE_API_URL", "http://localhost:8000")
    api_key = args.api_key or os.environ.get("ART_GUIDE_API_KEY") or os.environ.get("API_BEARER_TOKEN")

    exit_code, _, _ = run_eval(
        api_url=api_url,
        api_key=api_key,
        dataset_path=Path(args.dataset) if args.dataset else DATASET_PATH,
        thresholds_path=Path(args.thresholds) if args.thresholds else THRESHOLDS_PATH,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        in_catalog_only=args.in_catalog_only,
        request_delay_s=args.request_delay_s,
        dry_run=args.dry_run,
    )
    return exit_code


# --------------------------------------------------------------------- parser

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ml.cli",
        description="art-guide ML utilities: ingestion + eval data augmentation.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{ingest,augment,embed,eval}")

    # ingest
    ingest = sub.add_parser("ingest", help="Run an ingestion adapter.")
    ingest_sub = ingest.add_subparsers(dest="source", metavar="{met}")

    met = ingest_sub.add_parser(
        "met", help="Ingest from The Met Open Access collection."
    )
    met.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max number of records to successfully ingest. "
            "Default: 100 for the DB path; unlimited for --jsonl-out."
        ),
    )
    met.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run fetch + image download + embed end-to-end but skip DB writes. "
            "Useful when no Postgres is available."
        ),
    )
    met.add_argument(
        "--database-url",
        default=None,
        help=(
            "Postgres DSN. Defaults to $DATABASE_URL or the local "
            "docker-compose stack."
        ),
    )
    met.add_argument(
        "--batch-commit-size",
        type=int,
        default=None,
        help="Rows per DB transaction (default: 50).",
    )
    met.add_argument(
        "--department-ids",
        type=str,
        default=None,
        help=(
            "Comma-separated Met departmentIds to pre-filter the candidate "
            "pool (e.g. '11,21' for European Paintings + Modern/Contemporary "
            "Sculpture). Strongly recommended at scale: without it, the "
            "global /objects feed mixes in non-painting/sculpture records "
            "that get filtered client-side, wasting requests."
        ),
    )
    met.add_argument(
        "--jsonl-out",
        type=str,
        default=None,
        help=(
            "Legacy mode: write normalized records as JSONL to this path "
            "instead of upserting into Postgres. No embeddings are computed."
        ),
    )
    met.set_defaults(func=_cmd_ingest_met)

    # augment
    aug = sub.add_parser(
        "augment", help="Generate augmented variants of a reference artwork image."
    )
    aug.add_argument("--source", required=True, help="Source image path.")
    aug.add_argument(
        "--artwork-id",
        required=True,
        help="Source-prefixed id (e.g. 'met:436532').",
    )
    aug.add_argument(
        "--out",
        default="evals/datasets/augmented/",
        help="Output directory (default: evals/datasets/augmented/).",
    )
    aug.add_argument(
        "--count", type=int, default=10, help="Number of variants (default: 10)."
    )
    aug.set_defaults(func=_cmd_augment)

    # embed
    embed = sub.add_parser(
        "embed",
        help="Embed a single image and print the shape + head of the vector.",
    )
    embed.add_argument(
        "image",
        help="Path to a JPEG/PNG/HEIC image to embed.",
    )
    embed.add_argument(
        "--model",
        default=None,
        help=(
            "Embedder name (default: the Phase 1 winner from "
            "docs/embeddings.md). See ml.embeddings.list_specs()."
        ),
    )
    embed.add_argument(
        "--head",
        type=int,
        default=8,
        help="Print the first N values of the vector (default: 8).",
    )
    embed.set_defaults(func=_cmd_embed)

    # eval
    ev = sub.add_parser(
        "eval",
        help="Run the retrieval eval harness against a live API server.",
    )
    ev.add_argument(
        "--api-url",
        default=None,
        help="Base URL of the API (default: $ART_GUIDE_API_URL or http://localhost:8000).",
    )
    ev.add_argument(
        "--api-key",
        default=None,
        help="Bearer token (default: $ART_GUIDE_API_KEY or $API_BEARER_TOKEN).",
    )
    ev.add_argument(
        "--dataset",
        default=None,
        help="Path to dataset.jsonl (default: services/ml/eval/dataset.jsonl).",
    )
    ev.add_argument(
        "--thresholds",
        default=None,
        help="Path to thresholds.yaml (default: services/ml/eval/thresholds.yaml).",
    )
    ev.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write JSON report + markdown (default: dataset directory).",
    )
    ev.add_argument(
        "--in-catalog-only",
        action="store_true",
        help="Skip out_of_catalog cases (faster, no Wikimedia downloads).",
    )
    ev.add_argument(
        "--request-delay-s",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep between requests. "
            "Use 32+ to stay under the default 10 req/5 min rate limit."
        ),
    )
    ev.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cases that would run without making HTTP requests.",
    )
    ev.set_defaults(func=_cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    argv = sys.argv[1:] if argv is None else argv

    parser = _build_parser()
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
