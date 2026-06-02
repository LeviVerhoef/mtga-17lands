"""
analysis/synergy.py

Win-rate conditioned synergy between card pairs.
"This card wins more when paired with X."

For each pair (X, Y):
  wr_with    = win rate of decks containing both X and Y
  wr_without = win rate of decks containing X but not Y
  delta      = wr_with - wr_without
  n          = sample size (games with both)

Output (data/artifacts/<SET>.<FORMAT>.synergy.parquet):
  card_x, card_y, wr_with, wr_without, delta, n
  (only rows where n >= min_games and |delta| >= min_delta)

Implementation note: computed as a few set-based SQL passes. Each game's deck
is UNPIVOTed into a long present(game_id, card, won) table; a single self-join
counts wins for every co-occurring pair at once. The "without" stats are derived
by subtracting the pair counts from each card's overall totals, so we never scan
the table per pair. The naive per-pair version would issue ~76k full-table scans.
"""

import logging
from pathlib import Path

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_MIN_GAMES = 100
_MIN_DELTA = 0.02   # only store pairs with >= 2% win-rate difference

# Basic lands appear in nearly every matching deck, so the "without" complement
# is tiny and degenerate — they are noise as synergy partners, not signal.
_BASICS = (
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
)


def compute(
    expansion: str,
    event_type: str,
    ingestor: DatasetIngestor | None = None,
    min_games: int = _MIN_GAMES,
    min_delta: float = _MIN_DELTA,
) -> Path:
    """Compute and save synergy parquet. Returns output path."""
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = _ARTIFACTS / f"{expansion}.{event_type}.synergy.parquet"

    ing = ingestor or DatasetIngestor()
    tname = ing.load_into_db(expansion, event_type, "game_data")
    con = ing.connection(expansion, event_type)

    cols = [row[0] for row in con.execute(f'DESCRIBE "{tname}"').fetchall()]
    deck_cols = [c for c in cols if c.startswith("deck_")]
    if not deck_cols:
        raise ValueError(f"No deck_* columns in {tname}. Verify game_data schema.")

    deck_col_list = ", ".join(f'"{c}"' for c in deck_cols)
    prefix_len = len("deck_")
    logger.info("Computing synergy for %d cards in %s.%s...", len(deck_cols), expansion, event_type)

    # 1. One row per game with a stable id + won flag + deck columns.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _games AS
        SELECT ROW_NUMBER() OVER () AS game_id, CAST(won AS INT) AS won, {deck_col_list}
        FROM "{tname}"
    """)

    # 2. Long form: one row per (game, card-in-deck), carrying the win flag.
    #    Basic lands are dropped — they are in almost every deck and only add noise.
    basics_list = ", ".join(f"'{b}'" for b in _BASICS)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _present AS
        SELECT game_id, won, substr(name, {prefix_len + 1}) AS card
        FROM (
            UNPIVOT _games
            ON {deck_col_list}
            INTO NAME name VALUE cnt
        )
        WHERE cnt > 0
          AND substr(name, {prefix_len + 1}) NOT IN ({basics_list})
    """)

    # 3. Per-card totals: games containing the card and wins among them.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _cardstats AS
        SELECT card, COUNT(*) AS n_x, SUM(won) AS wins_x
        FROM _present GROUP BY card
    """)

    # 4. Every co-occurring pair's "with" stats in one self-join.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _pairs AS
        SELECT a.card AS cx, b.card AS cy,
               COUNT(*) AS n_with, SUM(a.won) AS wins_with
        FROM _present a
        JOIN _present b ON a.game_id = b.game_id AND a.card <> b.card
        GROUP BY a.card, b.card
        HAVING COUNT(*) >= {min_games}
    """)

    # 5. Derive "without" stats by subtraction; keep meaningful deltas.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _synergy AS
        SELECT card_x, card_y, wr_with, wr_without, (wr_with - wr_without) AS delta, n
        FROM (
            SELECT
                p.cx AS card_x,
                p.cy AS card_y,
                p.wins_with::DOUBLE / p.n_with AS wr_with,
                CASE WHEN (cs.n_x - p.n_with) > 0
                     THEN (cs.wins_x - p.wins_with)::DOUBLE / (cs.n_x - p.n_with)
                END AS wr_without,
                p.n_with AS n
            FROM _pairs p
            JOIN _cardstats cs ON cs.card = p.cx
            WHERE cs.n_x >= {min_games}
              AND (cs.n_x - p.n_with) >= {min_games}  -- trustworthy "without" sample
        )
        WHERE wr_without IS NOT NULL
          AND abs(wr_with - wr_without) >= {min_delta}
    """)

    n_pairs = con.execute("SELECT COUNT(*) FROM _synergy").fetchone()[0]
    con.execute(f"COPY (SELECT * FROM _synergy) TO '{out}' (FORMAT PARQUET)")

    logger.info(
        "Synergy: %d pairs (|delta|>=%.2f, n>=%d) -> %s",
        n_pairs, min_delta, min_games, out.name,
    )
    return out
