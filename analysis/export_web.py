"""
analysis/export_web.py

Task A of the Overwolf overlay plan (docs/06-overwolf-overlay-plan.md): convert
the committed parquet artifacts for a set into ONE compact JSON the JS front-end
can load instantly. No Overwolf needed — this runs and is tested on any platform.

Output: web/data/<SET>.<FORMAT>.context.json

Compact format (card names are interned into an index to keep the file small):
{
  "set": "SOS", "format": "PremierDraft", "generated_at": "...",
  "schema": {
    "metrics": "idx -> [gihwr, alsa, ata, ohwr, gdwr, gpwr, gih_count]",
    "trophy":  "idx -> [rate_delta, ata_delta, seen_trophy]",
    "cooc":    "idx -> [[partner_idx, lift], ...]   (top partners, lift desc)",
    "synergy": "idx -> [[partner_idx, delta], ...]  (top partners, |delta| desc)"
  },
  "cards":   ["Card A", "Card B", ...],   # index -> card name
  "metrics": { "0": [0.601, 4.25, 5.26, 0.617, 0.592, 0.587, 45613], ... },
  "trophy":  { "0": [0.036, -0.4, 1461], ... },
  "cooc":    { "0": [[12, 2.37], [5, 2.09], ...], ... },
  "synergy": { "0": [[7, 0.099], [3, 0.094], ...], ... }
}

The JS overlay reconstructs the same signals analysis.context_advisor computes:
- trophy delta for the card,
- average co-occurrence lift of the card with the cards in your pool,
- total positive win-rate synergy of the card with your pool.

Co-occurrence mirrors the runtime preference in context_advisor (trophy file
first, then all). Partner lists keep ALL pairs by default (sorted strongest
first) so the panel can compute the exact same pool-lift / pool-synergy the
Python engine does for any pool card; the bundles are still only ~0.3-0.5 MB/set.
Pass max_partners to truncate if size ever matters.
"""

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_WEB_DIR = Path(__file__).parent.parent / "web" / "data"

# Partner lists per card (co-occurrence / synergy). None = keep all pairs, which
# preserves exact fidelity with context_advisor's pool math. Set an int to cap.
MAX_PARTNERS = None
# Drop trophy rows with too few trophy observations to be trustworthy
# (mirrors context_advisor._MIN_TROPHY_SEEN).
MIN_TROPHY_SEEN = 50


def _round(x, n):
    return None if x is None else round(float(x), n)


def export_web(
    expansion: str,
    event_type: str,
    artifacts_dir: Path | None = None,
    out_dir: Path | None = None,
    max_partners: int | None = MAX_PARTNERS,
) -> Path:
    """Build the compact context JSON for one set/format. Returns the path."""
    art = artifacts_dir or _ARTIFACTS
    out = out_dir or _WEB_DIR
    out.mkdir(parents=True, exist_ok=True)

    trophy_path = art / f"{expansion}.{event_type}.trophy_pick_stats.parquet"
    synergy_path = art / f"{expansion}.{event_type}.synergy.parquet"
    metrics_path = art / f"{expansion}.{event_type}.card_metrics.parquet"
    # Prefer trophy co-occurrence, fall back to all (same as context_advisor).
    cooc_path = art / f"{expansion}.{event_type}.cooccurrence.trophy.parquet"
    if not cooc_path.exists():
        cooc_path = art / f"{expansion}.{event_type}.cooccurrence.all.parquet"

    if not trophy_path.exists():
        raise FileNotFoundError(
            f"No artifacts for {expansion}.{event_type} in {art} "
            f"(missing {trophy_path.name}). Run analysis.export first."
        )

    con = duckdb.connect()

    # --- Card index: union of every card name we reference ---
    names: set[str] = set()
    trophy_rows = con.execute(
        f"SELECT card_name, pick_rate_delta, ata_delta, seen_trophy "
        f"FROM read_parquet('{trophy_path}')"
    ).fetchall()
    for r in trophy_rows:
        names.add(r[0])

    cooc_rows = []
    if cooc_path.exists():
        cooc_rows = con.execute(
            f"SELECT card_x, card_y, lift FROM read_parquet('{cooc_path}')"
        ).fetchall()
        for r in cooc_rows:
            names.add(r[0]); names.add(r[1])

    syn_rows = []
    if synergy_path.exists():
        syn_rows = con.execute(
            f"SELECT card_x, card_y, delta FROM read_parquet('{synergy_path}')"
        ).fetchall()
        for r in syn_rows:
            names.add(r[0]); names.add(r[1])

    metrics_rows = []
    if metrics_path.exists():
        metrics_rows = con.execute(
            f"SELECT card_name, gihwr, alsa, ata, ohwr, gdwr, gpwr, gih_count "
            f"FROM read_parquet('{metrics_path}')"
        ).fetchall()
        for r in metrics_rows:
            if r[0]:
                names.add(r[0])
    con.close()

    cards = sorted(names)
    idx = {name: i for i, name in enumerate(cards)}

    # --- Trophy: idx -> [rate_delta, ata_delta, seen_trophy] ---
    trophy: dict[str, list] = {}
    for card_name, rate_delta, ata_delta, seen_trophy in trophy_rows:
        if (seen_trophy or 0) < MIN_TROPHY_SEEN:
            continue
        if rate_delta is None and ata_delta is None:
            continue
        trophy[str(idx[card_name])] = [
            _round(rate_delta, 4), _round(ata_delta, 3), int(seen_trophy or 0)
        ]

    # --- Co-occurrence: idx_x -> top [[idx_y, lift], ...] by lift desc ---
    cooc = _adjacency(cooc_rows, idx, max_partners, value_round=3, by_abs=False)

    # --- Synergy: idx_x -> top [[idx_y, delta], ...] by |delta| desc ---
    synergy = _adjacency(syn_rows, idx, max_partners, value_round=4, by_abs=True)

    # --- Metrics: idx -> [gihwr, alsa, ata, ohwr, gdwr, gpwr, gih_count] ---
    metrics: dict[str, list] = {}
    for card_name, gihwr, alsa, ata, ohwr, gdwr, gpwr, gih_count in metrics_rows:
        if not card_name:
            continue
        metrics[str(idx[card_name])] = [
            _round(gihwr, 4), _round(alsa, 2), _round(ata, 2),
            _round(ohwr, 4), _round(gdwr, 4), _round(gpwr, 4),
            int(gih_count or 0),
        ]

    bundle = {
        "set": expansion,
        "format": event_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": {
            "metrics": "idx -> [gihwr, alsa, ata, ohwr, gdwr, gpwr, gih_count]",
            "trophy": "idx -> [rate_delta, ata_delta, seen_trophy]",
            "cooc": "idx -> [[partner_idx, lift], ...] (lift desc)",
            "synergy": "idx -> [[partner_idx, delta], ...] (|delta| desc)",
        },
        "cards": cards,
        "metrics": metrics,
        "trophy": trophy,
        "cooc": cooc,
        "synergy": synergy,
    }

    out_path = out / f"{expansion}.{event_type}.context.json"
    with out_path.open("w") as f:
        json.dump(bundle, f, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    logger.info(
        "Wrote %s (%.0f KB): %d cards, %d metrics, %d trophy, %d cooc-src, %d syn-src",
        out_path.name, size_kb, len(cards), len(metrics), len(trophy), len(cooc), len(synergy),
    )
    return out_path


def _adjacency(rows, idx, max_partners, value_round, by_abs):
    """Group (card_x, card_y, value) rows into idx_x -> top partner list."""
    grouped: dict[int, list] = {}
    for cx, cy, val in rows:
        grouped.setdefault(idx[cx], []).append((idx[cy], val))
    out: dict[str, list] = {}
    keyfn = (lambda p: abs(p[1])) if by_abs else (lambda p: p[1])
    for xi, partners in grouped.items():
        partners.sort(key=keyfn, reverse=True)
        kept = partners if max_partners is None else partners[:max_partners]
        out[str(xi)] = [[yi, _round(v, value_round)] for yi, v in kept]
    return out


def _discover_sets(art: Path) -> list[tuple[str, str]]:
    """Find (expansion, event_type) pairs that have trophy artifacts."""
    found = []
    for p in sorted(art.glob("*.trophy_pick_stats.parquet")):
        stem = p.name[: -len(".trophy_pick_stats.parquet")]
        if "." in stem:
            expansion, event_type = stem.split(".", 1)
            found.append((expansion, event_type))
    return found


def _cli():
    parser = argparse.ArgumentParser(description="Export compact web context JSON from artifacts")
    parser.add_argument("--expansion", help="e.g. SOS (omit with --all)")
    parser.add_argument("--event-type", default="PremierDraft")
    parser.add_argument("--all", action="store_true", help="Export every set found in data/artifacts/")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.all:
        pairs = _discover_sets(_ARTIFACTS)
        if not pairs:
            logger.warning("No artifacts found in %s", _ARTIFACTS)
        for expansion, event_type in pairs:
            export_web(expansion, event_type)
    elif args.expansion:
        export_web(args.expansion, args.event_type)
    else:
        parser.error("provide --expansion <SET> or --all")


if __name__ == "__main__":
    _cli()
