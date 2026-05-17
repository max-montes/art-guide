"""Command-line entry point for the art-guide ML service.

Usage examples
--------------
    art-guide-ml ingest met --limit 100
    art-guide-ml ingest met --limit 5 --dry-run
    art-guide-ml ingest met --limit 200 --jsonl-out data/met/normalized.jsonl
    art-guide-ml ingest aic --limit 100
    art-guide-ml ingest aic --limit 0 --request-delay 1.05 --batch-commit-size 64
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
from ml.ingest.met_db import DEFAULT_REQUEST_DELAY_S
from ml.ingest.rijks_db import (
    DEFAULT_REQUEST_DELAY_S as RIJKS_DEFAULT_REQUEST_DELAY_S,
    DEFAULT_SET_SPECS as RIJKS_DEFAULT_SET_SPECS,
)
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

    # --limit 0 (or unset and using default) means "unlimited" for sharded runs.
    # The downstream ingest_met_to_db treats None / non-positive as unbounded.
    limit = args.limit if args.limit is not None else 100
    batch_size = args.batch_commit_size or DEFAULT_BATCH_COMMIT_SIZE
    department_ids = (
        [int(x) for x in args.department_ids.split(",") if x.strip()]
        if args.department_ids
        else None
    )

    shard_count = max(1, int(args.shard_count or 1))
    shard_index = _resolve_shard_index(args.shard_index, shard_count)
    request_delay = (
        float(args.request_delay)
        if args.request_delay is not None
        else DEFAULT_REQUEST_DELAY_S
    )

    if shard_count > 1:
        logger.info(
            "sharded ingest: shard_index=%d shard_count=%d request_delay=%.3fs",
            shard_index,
            shard_count,
            request_delay,
        )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                command_timeout=30,
                timeout=15,
            )
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
            request_delay=request_delay,
            shard_index=shard_index,
            shard_count=shard_count,
            dry_run=args.dry_run,
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    shard_tag = f" shard={shard_index}/{shard_count}" if shard_count > 1 else ""
    print(
        f"[{mode}{shard_tag}] Met ingest done: "
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


def _cmd_ingest_aic(args: argparse.Namespace) -> int:
    """Fetch AIC records, embed, and UPSERT into Postgres."""
    return asyncio.run(_ingest_aic_to_db(args))


async def _ingest_aic_to_db(args: argparse.Namespace) -> int:
    """DB path: listing-walk → preprocess → embed (SigLIP D=768) → UPSERT."""
    import asyncpg

    from ml.ingest.aic_db import (
        DEFAULT_BATCH_COMMIT_SIZE as AIC_DEFAULT_BATCH_COMMIT_SIZE,
        DEFAULT_PAGE_SIZE as AIC_DEFAULT_PAGE_SIZE,
        DEFAULT_REQUEST_DELAY_S as AIC_DEFAULT_REQUEST_DELAY_S,
        ingest_aic_to_db,
    )

    limit = args.limit if args.limit is not None else 100
    batch_size = args.batch_commit_size or AIC_DEFAULT_BATCH_COMMIT_SIZE
    page_size = args.page_size or AIC_DEFAULT_PAGE_SIZE
    start_page = args.start_page or 1
    request_delay = (
        float(args.request_delay)
        if args.request_delay is not None
        else AIC_DEFAULT_REQUEST_DELAY_S
    )

    if request_delay < 1.0:
        logger.warning(
            "request_delay=%.3fs is below AIC's published 1 req/s cap. "
            "Expect 429s and IP throttling. Raise to 1.05 to be safe.",
            request_delay,
        )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                command_timeout=30,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not open Postgres pool ({exc!s}). "
                "Pass --dry-run to skip DB writes, or bring up "
                "infra/docker-compose.yml first.",
                file=sys.stderr,
            )
            return 2

    try:
        stats = await ingest_aic_to_db(
            pool=pool,
            limit=limit,
            batch_commit_size=batch_size,
            request_delay=request_delay,
            page_size=page_size,
            start_page=start_page,
            dry_run=args.dry_run,
            resume_skip_existing=bool(getattr(args, "resume_skip_existing", False)),
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    print(
        f"[{mode}] AIC ingest done: "
        f"pages_walked={summary['pages_walked']}, "
        f"candidates={summary['candidate_ids']}, "
        f"fetched={summary['fetched']}, "
        f"persisted={summary['total_persisted']} "
        f"(inserted={summary['inserted']}, updated={summary['updated']}), "
        f"skipped_filter={summary['skipped_filter']}, "
        f"skipped_existing={summary['skipped_existing']}, "
        f"skipped_image={summary['skipped_image_error']}, "
        f"skipped_embed={summary['skipped_embed_error']}, "
        f"skipped_db={summary['skipped_db_error']}, "
        f"rate_limited={summary['rate_limited_events']}"
    )
    return 0


def _cmd_ingest_rijks(args: argparse.Namespace) -> int:
    """Fetch Rijks OAI-PMH records, embed, and UPSERT into Postgres."""
    return asyncio.run(_ingest_rijks_to_db(args))


# ----------------------------------------------------------------- dump paths

def _cmd_ingest_met_dump(args: argparse.Namespace) -> int:
    """v2 Met ingest via the metmuseum/openaccess CSV dump."""
    return asyncio.run(_ingest_met_dump_to_db(args))


async def _ingest_met_dump_to_db(args: argparse.Namespace) -> int:
    import asyncpg

    from ml.ingest.met_csv import (
        DEFAULT_CACHE_MAX_AGE_DAYS as MET_CSV_CACHE_MAX_AGE_DAYS,
        DEFAULT_EMBED_BATCH_SIZE as MET_CSV_DEFAULT_EMBED_BATCH,
        ingest_met_csv_to_db,
    )
    from ml.ingest.met_db import (
        DEFAULT_BATCH_COMMIT_SIZE as MET_DEFAULT_BATCH_COMMIT_SIZE,
    )

    limit = args.limit if args.limit is not None else 0
    batch_commit_size = args.batch_commit_size or MET_DEFAULT_BATCH_COMMIT_SIZE
    embed_batch_size = args.embed_batch_size or MET_CSV_DEFAULT_EMBED_BATCH
    cache_max_age_days = (
        args.cache_max_age_days
        if args.cache_max_age_days is not None
        else MET_CSV_CACHE_MAX_AGE_DAYS
    )
    request_delay = (
        float(args.request_delay)
        if args.request_delay is not None
        else DEFAULT_REQUEST_DELAY_S
    )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                command_timeout=30,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not open Postgres pool ({exc!s}). "
                "Pass --dry-run to skip DB writes, or bring up "
                "infra/docker-compose.yml first.",
                file=sys.stderr,
            )
            return 2

    try:
        stats = await ingest_met_csv_to_db(
            pool=pool,
            limit=limit,
            max_records=args.max_records,
            batch_commit_size=batch_commit_size,
            embed_batch_size=embed_batch_size,
            request_delay=request_delay,
            cache_max_age_days=cache_max_age_days,
            force_refresh_csv=args.force_refresh,
            dry_run=args.dry_run,
            resume_skip_existing=bool(getattr(args, "resume_skip_existing", False)),
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    print(
        f"[{mode}] Met-dump ingest done: "
        f"csv_rows={summary['csv_rows_total']}, "
        f"csv_accepted={summary['csv_rows_accepted']}, "
        f"csv_skipped={summary['csv_rows_skipped_filter']}, "
        f"fetched={summary['fetched']}, "
        f"persisted={summary['total_persisted']} "
        f"(inserted={summary['inserted']}, updated={summary['updated']}), "
        f"skipped_existing={summary['skipped_existing']}, "
        f"skipped_api={summary['skipped_api_error']}, "
        f"skipped_filter={summary['skipped_filter']}, "
        f"skipped_image={summary['skipped_image_error']}, "
        f"skipped_embed={summary['skipped_embed_error']}, "
        f"skipped_db={summary['skipped_db_error']}, "
        f"rate_limited={summary['rate_limited_events']}"
    )
    return 0


def _cmd_ingest_aic_dump(args: argparse.Namespace) -> int:
    """v2 AIC ingest via the art-institute-of-chicago/api-data Git repo."""
    return asyncio.run(_ingest_aic_dump_to_db(args))


async def _ingest_aic_dump_to_db(args: argparse.Namespace) -> int:
    import asyncpg

    from ml.ingest.aic_db import (
        DEFAULT_BATCH_COMMIT_SIZE as AIC_DEFAULT_BATCH_COMMIT_SIZE,
    )
    from ml.ingest.aic_dump import (
        DEFAULT_CACHE_MAX_AGE_DAYS as AIC_DUMP_CACHE_MAX_AGE_DAYS,
        DEFAULT_EMBED_BATCH_SIZE as AIC_DUMP_DEFAULT_EMBED_BATCH,
        DEFAULT_IMAGE_REQUEST_DELAY_S as AIC_DUMP_DEFAULT_IMG_DELAY,
        ingest_aic_dump_to_db,
    )

    limit = args.limit if args.limit is not None else 0
    batch_commit_size = args.batch_commit_size or AIC_DEFAULT_BATCH_COMMIT_SIZE
    embed_batch_size = args.embed_batch_size or AIC_DUMP_DEFAULT_EMBED_BATCH
    cache_max_age_days = (
        args.cache_max_age_days
        if args.cache_max_age_days is not None
        else AIC_DUMP_CACHE_MAX_AGE_DAYS
    )
    image_request_delay = (
        float(args.image_request_delay)
        if args.image_request_delay is not None
        else AIC_DUMP_DEFAULT_IMG_DELAY
    )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                command_timeout=30,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not open Postgres pool ({exc!s}). "
                "Pass --dry-run to skip DB writes, or bring up "
                "infra/docker-compose.yml first.",
                file=sys.stderr,
            )
            return 2

    try:
        stats = await ingest_aic_dump_to_db(
            pool=pool,
            limit=limit,
            max_records=args.max_records,
            batch_commit_size=batch_commit_size,
            embed_batch_size=embed_batch_size,
            image_request_delay=image_request_delay,
            cache_max_age_days=cache_max_age_days,
            force_refresh_repo=args.force_refresh,
            dry_run=args.dry_run,
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    print(
        f"[{mode}] AIC-dump ingest done: "
        f"json_files={summary['json_files_total']}, "
        f"accepted={summary['json_files_accepted']}, "
        f"skipped_filter={summary['json_files_skipped_filter']}, "
        f"parse_errors={summary['json_files_skipped_parse_error']}, "
        f"persisted={summary['total_persisted']} "
        f"(inserted={summary['inserted']}, updated={summary['updated']}), "
        f"skipped_image={summary['skipped_image_error']}, "
        f"skipped_embed={summary['skipped_embed_error']}, "
        f"skipped_db={summary['skipped_db_error']}, "
        f"rate_limited={summary['rate_limited_events']}"
    )
    return 0


async def _ingest_rijks_to_db(args: argparse.Namespace) -> int:
    import asyncpg

    from ml.ingest.rijks_db import (
        DEFAULT_BATCH_COMMIT_SIZE as RIJKS_DEFAULT_BATCH_COMMIT_SIZE,
        ingest_rijks_to_db,
    )

    limit = args.limit if args.limit is not None else 0
    batch_size = args.batch_commit_size or RIJKS_DEFAULT_BATCH_COMMIT_SIZE
    request_delay = (
        float(args.request_delay)
        if args.request_delay is not None
        else RIJKS_DEFAULT_REQUEST_DELAY_S
    )
    set_specs = (
        tuple(s.strip() for s in args.set_specs.split(",") if s.strip())
        if args.set_specs
        else RIJKS_DEFAULT_SET_SPECS
    )

    pool = None
    if not args.dry_run:
        dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
        logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                command_timeout=30,
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not open Postgres pool ({exc!s}). "
                "Pass --dry-run to skip DB writes, or bring up "
                "infra/docker-compose.yml first.",
                file=sys.stderr,
            )
            return 2

    try:
        stats = await ingest_rijks_to_db(
            pool=pool,
            limit=limit,
            batch_commit_size=batch_size,
            set_specs=set_specs,
            request_delay=request_delay,
            dry_run=args.dry_run,
            resume_skip_existing=bool(getattr(args, "resume_skip_existing", False)),
        )
    finally:
        if pool is not None:
            await pool.close()

    summary = stats.as_dict()
    mode = "DRY-RUN" if args.dry_run else "DB"
    print(
        f"[{mode}] Rijks ingest done: "
        f"candidates={summary['candidate_records']}, "
        f"pages={summary['fetched_pages']}, "
        f"records={summary['fetched_records']}, "
        f"persisted={summary['total_persisted']} "
        f"(inserted={summary['inserted']}, updated={summary['updated']}), "
        f"skipped_filter={summary['skipped_filter']}, "
        f"skipped_existing={summary['skipped_existing']}, "
        f"skipped_image={summary['skipped_image_error']}, "
        f"skipped_embed={summary['skipped_embed_error']}, "
        f"skipped_db={summary['skipped_db_error']}, "
        f"rate_limited={summary['rate_limited_events']}"
    )
    return 0


def _resolve_shard_index(raw: str | None, shard_count: int) -> int:
    """Resolve --shard-index value.

    Accepts a non-negative integer, or the literal string ``"auto"`` which
    parses the trailing integer of ``CONTAINER_APP_REPLICA_NAME``. Azure
    Container Apps Jobs sets that env var to a value like
    ``art-guide-prod-ingest-brsioxp-bcde-0`` for replica 0. We extract the
    last ``-N`` chunk and modulo by shard_count to be safe across naming
    drift.
    """
    if raw is None:
        return 0
    raw_s = str(raw).strip().lower()
    if raw_s.isdigit():
        return int(raw_s) % shard_count
    if raw_s != "auto":
        raise ValueError(
            f"--shard-index must be a non-negative integer or 'auto', got {raw!r}"
        )

    replica_name = os.environ.get("CONTAINER_APP_REPLICA_NAME", "")
    if not replica_name:
        logger.warning(
            "--shard-index=auto but CONTAINER_APP_REPLICA_NAME is empty; "
            "defaulting to shard_index=0"
        )
        return 0
    # Walk the segments from the tail for the first all-digits chunk.
    for chunk in reversed(replica_name.split("-")):
        if chunk.isdigit():
            idx = int(chunk) % shard_count
            logger.info(
                "auto-resolved shard_index=%d from CONTAINER_APP_REPLICA_NAME=%r",
                idx,
                replica_name,
            )
            return idx
    # Fallback: hash the name into a shard so distinct replicas still
    # disjoint themselves rather than all stampeding shard 0.
    idx = abs(hash(replica_name)) % shard_count
    logger.warning(
        "could not parse numeric replica index from %r; "
        "falling back to hash-derived shard_index=%d",
        replica_name,
        idx,
    )
    return idx


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


# -------------------------------------------------------------------- backfill

async def _run_backfill_met(args: argparse.Namespace) -> int:
    """Re-fetch Met JSON for existing rows, UPDATE enrichment columns only."""
    import asyncpg

    from ml.ingest.met_backfill import backfill_met_enrichment

    dsn = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DSN
    logger.info("Opening asyncpg pool against %s", _scrub_dsn(dsn))
    try:
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=4,
            command_timeout=30,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"Could not open Postgres pool ({exc!s}). "
            "Bring up infra/docker-compose.yml first.",
            file=sys.stderr,
        )
        return 2

    try:
        stats = await backfill_met_enrichment(
            pool=pool,
            request_delay=args.request_delay,
        )
    finally:
        await pool.close()

    summary = stats.as_dict()
    print(
        f"[BACKFILL] Met enrichment done: "
        f"total_rows={summary['total_rows']}, "
        f"fetched={summary['fetched']}, "
        f"updated={summary['updated']}, "
        f"skipped={summary['skipped_fetch_error']}"
    )
    return 0


def _cmd_backfill_met(args: argparse.Namespace) -> int:
    return asyncio.run(_run_backfill_met(args))


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
    sub = parser.add_subparsers(dest="command", metavar="{ingest,backfill,augment,embed,eval}")

    # ingest
    ingest = sub.add_parser("ingest", help="Run an ingestion adapter.")
    ingest_sub = ingest.add_subparsers(dest="source", metavar="{met,met-dump,rijks,aic,aic-dump}")

    met = ingest_sub.add_parser(
        "met", help="Ingest from The Met Open Access collection."
    )
    met.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max number of records to successfully ingest. "
            "Pass 0 (or omit in a sharded run) for unlimited — process every "
            "candidate id in the (possibly sharded) list. "
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
        "--shard-count",
        type=int,
        default=1,
        help=(
            "Number of parallel shards across which to partition the "
            "candidate id list (modulo split). 1 = no sharding (default). "
            "Used together with --shard-index for Container Apps Jobs "
            "parallelism > 1."
        ),
    )
    met.add_argument(
        "--shard-index",
        type=str,
        default=None,
        help=(
            "This replica's shard index in [0, shard-count). Pass a literal "
            "integer or the string 'auto' to derive it from the "
            "CONTAINER_APP_REPLICA_NAME env var (Azure Container Apps Jobs)."
        ),
    )
    met.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help=(
            f"Floor (seconds) between successive Met API requests, per replica. "
            f"Default: {DEFAULT_REQUEST_DELAY_S}. Lower for parallel runs (e.g. "
            f"0.05 = 20 req/s/replica); raise if you see sustained 429s."
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

    # ingest aic
    aic = ingest_sub.add_parser(
        "aic", help="Ingest from The Art Institute of Chicago Open Access API."
    )
    aic.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max number of records to successfully ingest. "
            "Pass 0 for unlimited — walk every page in the listing. "
            "Default: 100 for the DB path."
        ),
    )
    aic.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run fetch + image download + embed end-to-end but skip DB writes."
        ),
    )
    aic.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (default: $DATABASE_URL or local docker-compose stack).",
    )
    aic.add_argument(
        "--batch-commit-size",
        type=int,
        default=None,
        help="Rows per DB transaction (default: 50).",
    )
    aic.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help=(
            "Floor (seconds) between successive AIC API/image requests. "
            "Default: 1.05 (~57 req/min, under AIC's published 60 req/min cap). "
            "Do NOT lower below 1.0 without explicit AIC engineering permission."
        ),
    )
    aic.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="Records per listing page (default: 100, AIC max).",
    )
    aic.add_argument(
        "--start-page",
        type=int,
        default=None,
        help=(
            "Listing page to start from (1-based, default: 1). "
            "Use to resume after a restart; idempotent upsert makes overlap safe."
        ),
    )
    aic.add_argument(
        "--resume-skip-existing",
        action="store_true",
        help=(
            "On startup, query the DB for the set of source_id values already "
            "present for source='aic' and skip records from the listing whose "
            "source_id is in that set (before any image download/embed). "
            "Use when restarting an interrupted overnight run so the adapter "
            "adds genuinely new rows instead of re-embedding existing ones."
        ),
    )
    aic.set_defaults(func=_cmd_ingest_aic)

    # ingest rijks
    rijks = ingest_sub.add_parser(
        "rijks", help="Ingest from the Rijksmuseum OAI-PMH (EDM) feed."
    )
    rijks.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max number of records to successfully ingest. "
            "Pass 0 (or omit) for unbounded — process every record in the "
            "configured sets (~7K candidates; ~5K with images)."
        ),
    )
    rijks.add_argument(
        "--dry-run",
        action="store_true",
        help="Run fetch + image download + embed end-to-end but skip DB writes.",
    )
    rijks.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN. Defaults to $DATABASE_URL or the local docker-compose stack.",
    )
    rijks.add_argument(
        "--batch-commit-size",
        type=int,
        default=None,
        help="Rows per DB transaction (default: 50).",
    )
    rijks.add_argument(
        "--set-specs",
        type=str,
        default=None,
        help=(
            "Comma-separated Rijks OAI setSpec values to harvest. "
            f"Default: {','.join(RIJKS_DEFAULT_SET_SPECS)} "
            "(paintings + sculptures)."
        ),
    )
    rijks.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help=(
            f"Floor (seconds) between successive Rijks OAI requests. "
            f"Default: {RIJKS_DEFAULT_REQUEST_DELAY_S} (~5 req/s). "
            "Rijks publishes no per-IP rate limit; keep conservative."
        ),
    )
    rijks.add_argument(
        "--resume-skip-existing",
        action="store_true",
        help=(
            "On startup, query the DB for the set of source_id values already "
            "present for source='rijks' and skip records from the OAI listing "
            "whose source_id is in that set (before any image download/embed). "
            "Use when restarting an interrupted run."
        ),
    )
    rijks.set_defaults(func=_cmd_ingest_rijks)

    # ingest met-dump (v2 CSV-based)
    met_dump = ingest_sub.add_parser(
        "met-dump",
        help=(
            "v2 Met ingest: read the metmuseum/openaccess CSV from a local "
            "cache, filter (PD + denylist), then fetch /objects/{id} + image "
            "for accepted rows. Batched MPS-aware embedding."
        ),
    )
    met_dump.add_argument(
        "--limit", type=int, default=None,
        help="Max records to successfully ingest. 0 / omitted = unlimited.",
    )
    met_dump.add_argument(
        "--max-records", type=int, default=None,
        help=(
            "Stop reading the CSV after this many ACCEPTED rows (post-filter). "
            "Use 20 for a smoke run."
        ),
    )
    met_dump.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + image download + embed end-to-end; skip DB writes.",
    )
    met_dump.add_argument(
        "--database-url", default=None,
        help="Postgres DSN (default: $DATABASE_URL or local docker-compose).",
    )
    met_dump.add_argument(
        "--batch-commit-size", type=int, default=None,
        help="Rows per DB transaction (default: 50).",
    )
    met_dump.add_argument(
        "--batch-size", "--embed-batch-size", dest="embed_batch_size",
        type=int, default=None,
        help=(
            "Number of images per SigLIP forward pass (default: 8, also "
            "settable via ART_GUIDE_EMBED_BATCH). On Apple Silicon MPS, "
            "batch=8 is roughly a 4-5x speedup vs. sequential embeds."
        ),
    )
    met_dump.add_argument(
        "--request-delay", type=float, default=None,
        help=(
            f"Floor (seconds) between Met API requests. "
            f"Default: {DEFAULT_REQUEST_DELAY_S}."
        ),
    )
    met_dump.add_argument(
        "--cache-max-age-days", type=int, default=None,
        help="Re-download the CSV if cache is older than this many days (default: 7).",
    )
    met_dump.add_argument(
        "--force-refresh", action="store_true",
        help="Always re-download the CSV regardless of cache age.",
    )
    met_dump.add_argument(
        "--resume-skip-existing",
        action="store_true",
        help=(
            "On startup, query the DB for the set of source_id values already "
            "present for source='met' and skip records from the CSV whose "
            "source_id is in that set (before any API fetch / image download). "
            "Use when restarting an interrupted ACA Job run so the adapter "
            "adds genuinely new rows instead of re-fetching existing ones."
        ),
    )
    met_dump.set_defaults(func=_cmd_ingest_met_dump)

    # ingest aic-dump (v2 git-cloned JSON tree)
    aic_dump = ingest_sub.add_parser(
        "aic-dump",
        help=(
            "v2 AIC ingest: walk the art-institute-of-chicago/api-data JSON "
            "tree from a local Git clone. Zero AIC API calls; images fetched "
            "from IIIF CDN. Batched MPS-aware embedding."
        ),
    )
    aic_dump.add_argument(
        "--limit", type=int, default=None,
        help="Max records to successfully ingest. 0 / omitted = unlimited.",
    )
    aic_dump.add_argument(
        "--max-records", type=int, default=None,
        help=(
            "Stop after this many ACCEPTED records (post-denylist). "
            "Use 20 for a smoke run."
        ),
    )
    aic_dump.add_argument(
        "--dry-run", action="store_true",
        help="Image download + embed end-to-end; skip DB writes.",
    )
    aic_dump.add_argument(
        "--database-url", default=None,
        help="Postgres DSN (default: $DATABASE_URL or local docker-compose).",
    )
    aic_dump.add_argument(
        "--batch-commit-size", type=int, default=None,
        help="Rows per DB transaction (default: 50).",
    )
    aic_dump.add_argument(
        "--batch-size", "--embed-batch-size", dest="embed_batch_size",
        type=int, default=None,
        help=(
            "Number of images per SigLIP forward pass (default: 8; also "
            "settable via ART_GUIDE_EMBED_BATCH)."
        ),
    )
    aic_dump.add_argument(
        "--image-request-delay", type=float, default=None,
        help=(
            "Floor (seconds) between successive IIIF image GETs. "
            "Default: 0.05 (~20 req/s). The IIIF CDN is not subject to "
            "AIC's 60 req/min API cap."
        ),
    )
    aic_dump.add_argument(
        "--cache-max-age-days", type=int, default=None,
        help="Re-pull the api-data repo if older than this many days (default: 7).",
    )
    aic_dump.add_argument(
        "--force-refresh", action="store_true",
        help="Always git-pull the api-data repo, regardless of cache age.",
    )
    aic_dump.set_defaults(func=_cmd_ingest_aic_dump)

    # backfill
    backfill = sub.add_parser(
        "backfill",
        help="Backfill enrichment fields without re-embedding.",
    )
    backfill_sub = backfill.add_subparsers(dest="source", metavar="{met}")

    backfill_met = backfill_sub.add_parser(
        "met",
        help=(
            "Re-fetch Met API JSON for existing rows and UPDATE the seven "
            "D-024 enrichment columns (artist_bio, credit_line, dimensions, "
            "dynasty, object_wikidata_url, date_begin, date_end). "
            "Embeddings are preserved. Idempotent."
        ),
    )
    backfill_met.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN (default: $DATABASE_URL or local docker-compose stack).",
    )
    backfill_met.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_S,
        help=f"Seconds between Met API requests (default: {DEFAULT_REQUEST_DELAY_S}).",
    )
    backfill_met.set_defaults(func=_cmd_backfill_met)
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
