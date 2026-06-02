"""
analysis/trophy.py

Computes trophy-conditioned pick statistics.
"In trophy decks of these colors, this card is taken more strongly."

Trophy = 7 match wins (event_match_wins == 7) in Premier/Traditional Draft.

Output schema (written to data/artifacts/<SET>.<FORMAT>.trophy_pick_stats.parquet):
  card_name, color_pair, pick_rate, ata, seen_count,
  pick_rate_all, ata_all, seen_count_all,
  pick_rate_delta, ata_delta
"""

import json
import logging
import os
import tempfile
from pathlib import Path

import duckdb

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_MIN_SEEN = 50  # minimum seen_count to include a row


def compute(
    expansion: str,
    event_type: str,
    ingestor: DatasetIngestor | None = None,
    min_seen: int = _MIN_SEEN,
) -> Path:
    """
    Compute trophy pick stats for the given set/format.
    Returns path to the written parquet file.
    """
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = _ARTIFACTS / f"{expansion}.{event_type}.trophy_pick_stats.parquet"

    ing = ingestor or DatasetIngestor()
    tname = ing.load_into_db(expansion, event_type, "draft_data")
    con = ing.connection(expansion, event_type)

    # Identify pack_card_* columns (one per card in the set)
    cols = [
        row[0]
        for row in con.execute(f"DESCRIBE \"{tname}\"").fetchall()
    ]
    pack_cols = [c for c in cols if c.startswith("pack_card_")]
    if not pack_cols:
        raise ValueError(
            f"No pack_card_* columns found in {tname}. "
            "Verify the dataset schema with ingest.dump_headers()."
        )

    # Build per-card stats for all decks and trophy decks
    # We'll do this in one SQL pass per subset to keep memory low.
    results = []
    for subset, filter_sql in [
        ("all", "1=1"),
        ("trophy", "event_match_wins = 7"),
    ]:
        card_stats = {}
        for col in pack_cols:
            card_name = col[len("pick_card_"):]  # strip prefix for display
            # pack_card_<Name> = 1 when the card was in the pack (seen)
            # pick = card_name when the card was picked
            safe_col = col.replace("'", "''")
            safe_name = col[len("pack_card_"):].replace("'", "''")
            row = con.execute(f"""
                SELECT
                    SUM(CASE WHEN "{safe_col}" > 0 THEN 1 ELSE 0 END) AS seen,
                    SUM(CASE WHEN pick = '{safe_name}' THEN 1 ELSE 0 END) AS taken,
                    AVG(CASE WHEN pick = '{safe_name}' THEN pack_number * 15 + pick_number ELSE NULL END) AS ata
                FROM "{tname}"
                WHERE {filter_sql}
            """).fetchone()
            seen, taken, ata = row
            if seen and seen >= min_seen:
                card_stats[safe_name] = {
                    "seen": seen,
                    "taken": taken or 0,
                    "pick_rate": (taken or 0) / seen,
                    "ata": ata,
                }
        results.append((subset, card_stats))

    all_stats = dict(results[0][1])
    trophy_stats = dict(results[1][1])

    rows = []
    all_cards = set(all_stats) | set(trophy_stats)
    for card in all_cards:
        a = all_stats.get(card, {})
        t = trophy_stats.get(card, {})
        rows.append({
            "card_name": card,
            "color_pair": "All",
            "seen_all": a.get("seen", 0),
            "pick_rate_all": a.get("pick_rate"),
            "ata_all": a.get("ata"),
            "seen_trophy": t.get("seen", 0),
            "pick_rate_trophy": t.get("pick_rate"),
            "ata_trophy": t.get("ata"),
            "pick_rate_delta": (
                (t["pick_rate"] - a["pick_rate"])
                if t.get("pick_rate") is not None and a.get("pick_rate") is not None
                else None
            ),
            "ata_delta": (
                (t["ata"] - a["ata"])
                if t.get("ata") is not None and a.get("ata") is not None
                else None
            ),
        })

    fd, tmpjson = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rows, f)
        tmp = duckdb.connect()
        tmp.execute(f"CREATE TABLE trophy_stats AS SELECT * FROM read_json_auto('{tmpjson}')")
        tmp.execute(f"COPY trophy_stats TO '{out}' (FORMAT PARQUET)")
        tmp.close()
    finally:
        os.unlink(tmpjson)

    logger.info("Wrote trophy pick stats: %s (%d cards)", out.name, len(rows))
    return out
