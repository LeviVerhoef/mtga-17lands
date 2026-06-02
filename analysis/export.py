"""
analysis/export.py

Orchestrates the full offline analytics pipeline for a set/format.
Runs ingest -> trophy -> cooccurrence -> synergy -> similar_pools
and writes a manifest to data/artifacts/<SET>.<FORMAT>.manifest.json.

Usage:
    python -m analysis.export --expansion BLB --event-type PremierDraft
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from analysis.ingest import DatasetIngestor
from analysis import trophy, cooccurrence, synergy, similar_pools, card_metrics

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"


def run_pipeline(
    expansion: str,
    event_type: str,
    force_download: bool = False,
    trophy_only_cooc: bool = True,
) -> dict:
    """
    Run the full pipeline. Returns the manifest dict.
    """
    start = time.time()
    ing = DatasetIngestor()
    manifest: dict = {
        "expansion": expansion,
        "event_type": event_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": {},
        "errors": {},
    }

    # 1. Ingest both datasets
    for kind in ("draft_data", "game_data"):
        try:
            ing.ensure_dataset(expansion, event_type, kind, force=force_download)
            ing.load_into_db(expansion, event_type, kind)
            manifest["artifacts"][kind] = "ok"
        except Exception as exc:
            logger.error("Ingest failed for %s: %s", kind, exc)
            manifest["errors"][kind] = str(exc)

    # 2. Trophy pick stats
    try:
        path = trophy.compute(expansion, event_type, ingestor=ing)
        manifest["artifacts"]["trophy_pick_stats"] = str(path)
    except Exception as exc:
        logger.error("Trophy computation failed: %s", exc)
        manifest["errors"]["trophy_pick_stats"] = str(exc)

    # 2b. Per-card metrics (GIHWR/OHWR/GDWR/GPWR/ALSA/ATA) from bulk
    try:
        path = card_metrics.compute(expansion, event_type, ingestor=ing)
        manifest["artifacts"]["card_metrics"] = str(path)
    except Exception as exc:
        logger.error("Card metrics computation failed: %s", exc)
        manifest["errors"]["card_metrics"] = str(exc)

    # 3. Co-occurrence (trophy pools and all pools)
    for t_only, label in [(True, "cooccurrence_trophy"), (False, "cooccurrence_all")]:
        try:
            path = cooccurrence.compute(expansion, event_type, trophy_only=t_only, ingestor=ing)
            manifest["artifacts"][label] = str(path)
        except Exception as exc:
            logger.error("%s failed: %s", label, exc)
            manifest["errors"][label] = str(exc)

    # 4. Synergy
    try:
        path = synergy.compute(expansion, event_type, ingestor=ing)
        manifest["artifacts"]["synergy"] = str(path)
    except Exception as exc:
        logger.error("Synergy computation failed: %s", exc)
        manifest["errors"]["synergy"] = str(exc)

    # 5. Similar pools (trophy and all)
    for t_only, label in [(True, "similar_pools_trophy"), (False, "similar_pools_all")]:
        try:
            vec_path, picks_path = similar_pools.build(expansion, event_type, trophy_only=t_only, ingestor=ing)
            manifest["artifacts"][label] = {"vectors": str(vec_path), "picks": str(picks_path)}
        except Exception as exc:
            logger.error("%s build failed: %s", label, exc)
            manifest["errors"][label] = str(exc)

    ing.close_all()

    manifest["elapsed_seconds"] = round(time.time() - start, 1)

    # Write manifest
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    manifest_path = _ARTIFACTS / f"{expansion}.{event_type}.manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2)
    logger.info("Pipeline done in %.0fs — manifest: %s", manifest["elapsed_seconds"], manifest_path)

    if manifest["errors"]:
        logger.warning("Errors in pipeline: %s", list(manifest["errors"].keys()))

    return manifest


def _cli():
    parser = argparse.ArgumentParser(description="Run offline analytics pipeline")
    parser.add_argument("--expansion", required=True, help="17Lands expansion code, e.g. BLB")
    parser.add_argument("--event-type", default="PremierDraft", help="e.g. PremierDraft, TradDraft")
    parser.add_argument("--force-download", action="store_true", help="Re-download even if cached")
    args = parser.parse_args()
    run_pipeline(args.expansion, args.event_type, force_download=args.force_download)


if __name__ == "__main__":
    _cli()
