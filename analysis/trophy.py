"""
analysis/trophy.py

Computes trophy-conditioned pick statistics.
"In trophy decks of these colors, this card is taken more strongly."

Trophy = 7 match wins (event_match_wins == 7) in Premier/Traditional Draft.

Output schema (data/artifacts/<SET>.<FORMAT>.trophy_pick_stats.parquet):
  card_name, color_pair,
  seen_all, pick_rate_all, ata_all,
  seen_trophy, pick_rate_trophy, ata_trophy,
  pick_rate_delta, ata_delta

Computed in two set-based SQL passes (taken/ATA by GROUP BY pick; seen counts
by UNPIVOT of the pack_card_* columns) rather than one query per card, which
on a full set would be ~550 full-table scans over millions of rows. The
set-based form also sidesteps per-card-name string interpolation, which is how
the previous version mis-escaped card names containing apostrophes.
"""

import logging
from pathlib import Path

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_MIN_SEEN = 50  # minimum seen_all to include a row


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

    cols = [row[0] for row in con.execute(f'DESCRIBE "{tname}"').fetchall()]
    pack_cols = [c for c in cols if c.startswith("pack_card_")]
    if not pack_cols:
        raise ValueError(
            f"No pack_card_* columns found in {tname}. "
            "Verify the dataset schema with ingest.dump_headers()."
        )
    pack_col_list = ", ".join(f'"{c}"' for c in pack_cols)
    pack_prefix = len("pack_card_")

    # Pass 1: taken count + average-taken-at (ATA) per picked card, overall and
    # within trophy drafts. seq = global pick index (pack*15 + pick).
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _taken AS
        SELECT
            pick AS card_name,
            COUNT(*)                                   AS taken_all,
            AVG(seq)                                   AS ata_all,
            COUNT(*) FILTER (WHERE wins = 7)           AS taken_trophy,
            AVG(seq) FILTER (WHERE wins = 7)           AS ata_trophy
        FROM (
            SELECT pick, event_match_wins AS wins,
                   (pack_number * 15 + pick_number) AS seq
            FROM "{tname}"
        )
        GROUP BY pick
    """)

    # Pass 2: seen count per card (pack_card_<X> > 0), overall and trophy.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _seen AS
        SELECT
            substr(name, {pack_prefix + 1}) AS card_name,
            COUNT(*)                         AS seen_all,
            COUNT(*) FILTER (WHERE wins = 7) AS seen_trophy
        FROM (
            UNPIVOT (SELECT event_match_wins AS wins, {pack_col_list} FROM "{tname}")
            ON {pack_col_list}
            INTO NAME name VALUE cnt
        )
        WHERE cnt > 0
        GROUP BY substr(name, {pack_prefix + 1})
    """)

    # Join and derive rates + trophy-vs-overall deltas.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _trophy AS
        SELECT
            s.card_name,
            'All' AS color_pair,
            s.seen_all,
            t.taken_all::DOUBLE / s.seen_all AS pick_rate_all,
            t.ata_all,
            s.seen_trophy,
            CASE WHEN s.seen_trophy > 0
                 THEN t.taken_trophy::DOUBLE / s.seen_trophy END AS pick_rate_trophy,
            t.ata_trophy,
            CASE WHEN s.seen_trophy > 0
                 THEN t.taken_trophy::DOUBLE / s.seen_trophy
                      - t.taken_all::DOUBLE / s.seen_all END AS pick_rate_delta,
            CASE WHEN t.ata_trophy IS NOT NULL AND t.ata_all IS NOT NULL
                 THEN t.ata_trophy - t.ata_all END AS ata_delta
        FROM _seen s
        JOIN _taken t ON t.card_name = s.card_name
        WHERE s.seen_all >= {min_seen}
    """)

    n = con.execute("SELECT COUNT(*) FROM _trophy").fetchone()[0]
    con.execute(f"COPY (SELECT * FROM _trophy) TO '{out}' (FORMAT PARQUET)")

    logger.info("Wrote trophy pick stats: %s (%d cards)", out.name, n)
    return out
