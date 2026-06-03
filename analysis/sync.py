"""
analysis/sync.py

Conditional refresh driver + manifest builder for the data-distribution pipeline
(docs/07-data-distribution-plan.md).

The low-cost guarantee: before rebuilding a set we issue a cheap HEAD request for
its S3 bulk dataset and only rebuild when the `Last-Modified` advanced past what
we last built from. Frozen sets (17Lands stops updating a set ~1-2 months after
release) are therefore never recomputed.

Pipeline per set: download+compute (analysis.export) -> web bundle
(analysis.export_web) -> manifest entry (sha256, bytes, source_last_modified,
state). The manifest (web/data/manifest.json) is what the client diffs to
download only changed bundles.

Usage:
    python3 -m analysis.sync --sets SOS BLB                 # refresh these
    python3 -m analysis.sync --refresh-existing             # re-check every set
                                                            # that already has a bundle
    python3 -m analysis.sync --manifest-only                # rebuild manifest only
    python3 -m analysis.sync --sets SOS --force             # rebuild regardless
"""

import argparse
import hashlib
import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from analysis import export, export_web
from analysis.ingest import _s3_url, _local_gz, _db_path  # canonical paths

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent.parent / "data"
_WEB_DIR = Path(__file__).parent.parent / "web" / "data"
_MANIFEST = _WEB_DIR / "manifest.json"
_HEADERS = {"User-Agent": "mtga-17lands/1.0 (github.com/LeviVerhoef/mtga-17lands)"}

# A set is "active" if its dataset moved within this many days; else "frozen".
_ACTIVE_DAYS = 45


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_state(source_last_modified: datetime | None, now: datetime | None = None,
               active_days: int = _ACTIVE_DAYS) -> str:
    """'active' if the dataset moved recently, else 'frozen'/'unknown'."""
    if source_last_modified is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    age_days = (now - source_last_modified).total_seconds() / 86400
    return "active" if age_days <= active_days else "frozen"


def load_manifest(path: Path = _MANIFEST) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"generated_at": None, "sets": []}


def _entry_key(expansion: str, event_type: str) -> tuple:
    return (expansion, event_type)


def manifest_index(manifest: dict) -> dict:
    return {_entry_key(e["set"], e["format"]): e for e in manifest.get("sets", [])}


def needs_rebuild(entry: dict | None, remote_lm: datetime | None,
                  bundle_path: Path, force: bool = False) -> bool:
    """Decide whether a set must be rebuilt."""
    if force:
        return True
    if not bundle_path.exists():
        return True
    if entry is None:
        return True
    if remote_lm is None:
        # Can't tell remotely; don't rebuild an existing bundle on a failed HEAD.
        return False
    prev = entry.get("source_last_modified")
    if not prev:
        return True
    try:
        prev_dt = datetime.fromisoformat(prev)
    except ValueError:
        return True
    return remote_lm > prev_dt


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def remote_last_modified(expansion: str, event_type: str, kind: str = "draft_data",
                         client: httpx.Client | None = None) -> datetime | None:
    """HEAD the S3 dataset and return its Last-Modified, or None on failure."""
    url = _s3_url(kind, expansion, event_type)
    owns = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        resp = client.head(url, headers=_HEADERS)
        if resp.status_code != 200:
            logger.warning("HEAD %s -> %s", url, resp.status_code)
            return None
        lm = resp.headers.get("Last-Modified")
        return parsedate_to_datetime(lm).astimezone(timezone.utc) if lm else None
    except Exception as exc:  # pragma: no cover - network error path
        logger.warning("HEAD failed for %s: %s", url, exc)
        return None
    finally:
        if owns:
            client.close()


# ---------------------------------------------------------------------------
# Manifest entry / build
# ---------------------------------------------------------------------------

def bundle_entry(expansion: str, event_type: str, source_lm: datetime | None,
                 base_url: str = "", web_dir: Path = _WEB_DIR,
                 now: datetime | None = None) -> dict:
    """Build a manifest entry for an already-written context bundle."""
    fname = f"{expansion}.{event_type}.context.json"
    bundle = web_dir / fname
    return {
        "set": expansion,
        "format": event_type,
        "state": file_state(source_lm, now=now),
        "source_last_modified": source_lm.isoformat() if source_lm else None,
        "context": {
            "file": fname,
            "url": (base_url.rstrip("/") + "/" + fname) if base_url else fname,
            "sha256": sha256_file(bundle),
            "bytes": bundle.stat().st_size,
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        },
    }


def write_manifest(entries: list[dict], path: Path = _MANIFEST,
                   now: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "sets": sorted(entries, key=lambda e: (e["set"], e["format"])),
    }
    path.write_text(json.dumps(manifest, indent=2))
    return path


def rebuild_manifest_from_disk(base_url: str = "", web_dir: Path = _WEB_DIR,
                               manifest_path: Path = _MANIFEST) -> Path:
    """Rebuild the manifest from whatever context bundles exist, preserving each
    set's recorded source_last_modified (no network)."""
    prev = manifest_index(load_manifest(manifest_path))
    entries = []
    for bundle in sorted(web_dir.glob("*.context.json")):
        stem = bundle.name[: -len(".context.json")]
        expansion, event_type = stem.split(".", 1)
        old = prev.get(_entry_key(expansion, event_type), {})
        lm = old.get("source_last_modified")
        lm_dt = datetime.fromisoformat(lm) if lm else None
        entries.append(bundle_entry(expansion, event_type, lm_dt, base_url, web_dir))
    return write_manifest(entries, manifest_path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def cleanup_set(expansion: str, event_type: str) -> None:
    """Delete the large local intermediates (bulk .csv.gz + DuckDB) for a set,
    keeping only the small committed artifacts + web bundle. Used for back-catalog
    builds and CI runners with limited disk."""
    for kind in ("draft_data", "game_data"):
        gz = _local_gz(kind, expansion, event_type)
        if gz.exists():
            gz.unlink()
    db = _db_path(expansion, event_type)
    if db.exists():
        db.unlink()
    logger.info("Cleaned up bulk + duckdb for %s.%s", expansion, event_type)


def sync(pairs: list[tuple[str, str]], base_url: str = "", force: bool = False,
         manifest_path: Path = _MANIFEST, web_dir: Path = _WEB_DIR,
         cleanup: bool = False) -> dict:
    """Refresh the given (expansion, event_type) pairs and rewrite the manifest.

    Returns a summary dict {built: [...], skipped: [...], failed: {...}}.
    """
    manifest = load_manifest(manifest_path)
    index = manifest_index(manifest)
    summary = {"built": [], "skipped": [], "failed": {}}

    with httpx.Client(timeout=600, follow_redirects=True) as client:
        for expansion, event_type in pairs:
            key = _entry_key(expansion, event_type)
            bundle = web_dir / f"{expansion}.{event_type}.context.json"
            remote_lm = remote_last_modified(expansion, event_type, client=client)

            if not needs_rebuild(index.get(key), remote_lm, bundle, force):
                logger.info("%s.%s up to date (last-modified %s) — skip",
                            expansion, event_type, remote_lm)
                summary["skipped"].append(f"{expansion}.{event_type}")
                continue

            try:
                logger.info("Rebuilding %s.%s (remote last-modified %s)",
                            expansion, event_type, remote_lm)
                export.run_pipeline(expansion, event_type)
                export_web.export_web(expansion, event_type)
                index[key] = bundle_entry(expansion, event_type, remote_lm, base_url, web_dir)
                summary["built"].append(f"{expansion}.{event_type}")
                if cleanup:
                    cleanup_set(expansion, event_type)
            except Exception as exc:
                logger.error("Refresh failed for %s.%s: %s", expansion, event_type, exc)
                summary["failed"][f"{expansion}.{event_type}"] = str(exc)
            # Persist the manifest after each set so a long back-catalog run is
            # resumable and progress isn't lost on interruption.
            write_manifest(list(index.values()), manifest_path)

    write_manifest(list(index.values()), manifest_path)
    logger.info("Sync done: %d built, %d skipped, %d failed",
                len(summary["built"]), len(summary["skipped"]), len(summary["failed"]))
    return summary


def _existing_pairs(web_dir: Path = _WEB_DIR) -> list[tuple[str, str]]:
    pairs = []
    for bundle in sorted(web_dir.glob("*.context.json")):
        stem = bundle.name[: -len(".context.json")]
        pairs.append(tuple(stem.split(".", 1)))
    return pairs


def _cli():
    p = argparse.ArgumentParser(description="Conditional refresh + manifest builder")
    p.add_argument("--sets", nargs="+", help="Expansion codes to refresh, e.g. SOS BLB")
    p.add_argument("--event-type", default="PremierDraft")
    p.add_argument("--refresh-existing", action="store_true",
                   help="Re-check every set that already has a bundle")
    p.add_argument("--manifest-only", action="store_true",
                   help="Rebuild manifest.json from existing bundles (no network/compute)")
    p.add_argument("--force", action="store_true", help="Rebuild regardless of Last-Modified")
    p.add_argument("--cleanup", action="store_true",
                   help="Delete each set's bulk .csv.gz + DuckDB after bundling (low disk)")
    p.add_argument("--base-url", default="", help="CDN base URL to prefix bundle paths")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.manifest_only:
        path = rebuild_manifest_from_disk(args.base_url)
        logger.info("Wrote %s", path)
        return

    if args.refresh_existing:
        pairs = _existing_pairs()
    elif args.sets:
        pairs = [(s, args.event_type) for s in args.sets]
    else:
        p.error("provide --sets, --refresh-existing, or --manifest-only")
    sync(pairs, base_url=args.base_url, force=args.force, cleanup=args.cleanup)


if __name__ == "__main__":
    _cli()
