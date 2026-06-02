"""
analysis/card_metrics.py

Per-card 17Lands-style metrics computed from the PUBLIC BULK datasets only —
never the discouraged card_ratings API (see docs/07-data-distribution-plan.md).

Calibrated against the live card_ratings endpoint: the win RATES match 17Lands'
definitions to ~1% (the small offset is snapshot timing — the public bulk file is
frozen weeks before the live site), and ALSA/ATA match to ~0.3 of a pick.

Definitions (verified 2026-06-02 against SOS card_ratings):
  GPWR  = win rate of games with the card in the deck            (deck_*)
  OHWR  = win rate of games with the card in the opening hand    (opening_hand_*)
  GDWR  = win rate of games where the card was drawn             (drawn_*)
  GIHWR = win rate of games where the card was in hand ever      (opening_hand OR drawn)
  ALSA  = average (1-indexed) pick at which the card was seen     (pack_card_*, count-weighted)
  ATA   = average (1-indexed) pick at which the card was taken    (pick == card)

Output (data/artifacts/<SET>.<FORMAT>.card_metrics.parquet):
  card_name, gihwr, gih_count, ohwr, oh_count, gdwr, gd_count,
  gpwr, gp_count, alsa, alsa_count, ata, ata_count
"""

import logging
from pathlib import Path

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"


def compute(
    expansion: str,
    event_type: str,
    ingestor: DatasetIngestor | None = None,
) -> Path:
    """Compute and save per-card metrics parquet. Returns output path."""
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = _ARTIFACTS / f"{expansion}.{event_type}.card_metrics.parquet"

    ing = ingestor or DatasetIngestor()
    gtab = ing.load_into_db(expansion, event_type, "game_data")
    dtab = ing.load_into_db(expansion, event_type, "draft_data")
    con = ing.connection(expansion, event_type)

    gcols = [r[0] for r in con.execute(f'DESCRIBE "{gtab}"').fetchall()]
    dcols = [r[0] for r in con.execute(f'DESCRIBE "{dtab}"').fetchall()]

    deck = [c for c in gcols if c.startswith("deck_")]
    oh = [c for c in gcols if c.startswith("opening_hand_")]
    drawn = [c for c in gcols if c.startswith("drawn_")]
    pack = [c for c in dcols if c.startswith("pack_card_")]
    if not (deck and oh and drawn and pack):
        raise ValueError("Missing expected card columns in game_data/draft_data.")

    def lst(cols):
        return ", ".join(f'"{c}"' for c in cols)

    # --- Win-rate families from game_data (one set-based pass each) ---
    # game id + win flag + all the card columns we unpivot.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _g AS
        SELECT ROW_NUMBER() OVER () AS game_id, CAST(won AS INT) AS won,
               {lst(deck)}, {lst(oh)}, {lst(drawn)}
        FROM "{gtab}"
    """)

    def winrate_table(name, cols, prefix_len):
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE {name} AS
            SELECT substr(name, {prefix_len + 1}) AS card,
                   COUNT(*) AS n, SUM(won) AS wins
            FROM (UNPIVOT (SELECT won, {lst(cols)} FROM _g) ON {lst(cols)} INTO NAME name VALUE cnt)
            WHERE cnt > 0
            GROUP BY substr(name, {prefix_len + 1})
        """)

    winrate_table("_gp", deck, len("deck_"))
    winrate_table("_oh", oh, len("opening_hand_"))
    winrate_table("_gd", drawn, len("drawn_"))

    # GIH = in opening hand OR drawn, counted once per (game, card).
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _gih AS
        WITH presence AS (
            SELECT game_id, won, substr(name, {len('opening_hand_') + 1}) AS card
            FROM (UNPIVOT (SELECT game_id, won, {lst(oh)} FROM _g) ON {lst(oh)} INTO NAME name VALUE cnt)
            WHERE cnt > 0
            UNION
            SELECT game_id, won, substr(name, {len('drawn_') + 1}) AS card
            FROM (UNPIVOT (SELECT game_id, won, {lst(drawn)} FROM _g) ON {lst(drawn)} INTO NAME name VALUE cnt)
            WHERE cnt > 0
        )
        SELECT card, COUNT(*) AS n, SUM(won) AS wins FROM presence GROUP BY card
    """)

    # --- ALSA / ATA from draft_data ---
    # ATA: average 1-indexed pick where the card was taken.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _ata AS
        SELECT pick AS card, AVG(pick_number + 1) AS ata, COUNT(*) AS n
        FROM "{dtab}" GROUP BY pick
    """)
    # ALSA: count-weighted average 1-indexed pick over pack appearances.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _alsa AS
        SELECT substr(name, {len('pack_card_') + 1}) AS card,
               SUM((pick_number + 1) * cnt)::DOUBLE / SUM(cnt) AS alsa,
               SUM(cnt) AS n
        FROM (UNPIVOT (SELECT pick_number, {lst(pack)} FROM "{dtab}") ON {lst(pack)} INTO NAME name VALUE cnt)
        WHERE cnt > 0
        GROUP BY substr(name, {len('pack_card_') + 1})
    """)

    # --- Join into one per-card row ---
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _metrics AS
        SELECT
            COALESCE(gih.card, gp.card, alsa.card, ata.card) AS card_name,
            gih.wins::DOUBLE / NULLIF(gih.n, 0) AS gihwr, gih.n AS gih_count,
            oh.wins::DOUBLE  / NULLIF(oh.n, 0)  AS ohwr,  oh.n  AS oh_count,
            gd.wins::DOUBLE  / NULLIF(gd.n, 0)  AS gdwr,  gd.n  AS gd_count,
            gp.wins::DOUBLE  / NULLIF(gp.n, 0)  AS gpwr,  gp.n  AS gp_count,
            alsa.alsa AS alsa, alsa.n AS alsa_count,
            ata.ata   AS ata,  ata.n  AS ata_count
        FROM _gih gih
        FULL OUTER JOIN _gp gp     ON gp.card   = gih.card
        FULL OUTER JOIN _oh oh     ON oh.card   = COALESCE(gih.card, gp.card)
        FULL OUTER JOIN _gd gd     ON gd.card   = COALESCE(gih.card, gp.card)
        FULL OUTER JOIN _alsa alsa ON alsa.card = COALESCE(gih.card, gp.card)
        FULL OUTER JOIN _ata ata   ON ata.card  = COALESCE(gih.card, gp.card, alsa.card)
    """)

    n = con.execute("SELECT COUNT(*) FROM _metrics").fetchone()[0]
    con.execute(f"COPY (SELECT * FROM _metrics) TO '{out}' (FORMAT PARQUET)")
    logger.info("Wrote card metrics: %s (%d cards)", out.name, n)
    return out
