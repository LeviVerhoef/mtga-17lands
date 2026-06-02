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
  (only rows where n >= min_games and |delta| is meaningful)
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
_MIN_GAMES = 100
_MIN_DELTA = 0.02   # only store pairs with >= 2% win-rate difference


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

    card_names = [c[len("deck_"):] for c in deck_cols]
    logger.info("Computing synergy for %d cards in %s.%s...", len(card_names), expansion, event_type)

    rows = []
    for i, cx in enumerate(card_names):
        col_x = deck_cols[i]

        # Baseline: win rate of decks containing X
        baseline = con.execute(f"""
            SELECT AVG(CAST(won AS INT))::DOUBLE, COUNT(*)
            FROM "{tname}"
            WHERE "{col_x}" > 0
        """).fetchone()
        wr_x_baseline, n_x = baseline
        if not n_x or n_x < min_games:
            continue

        for j, cy in enumerate(card_names):
            if i == j:
                continue
            col_y = deck_cols[j]

            res = con.execute(f"""
                SELECT
                    AVG(CASE WHEN "{col_y}" > 0 THEN CAST(won AS INT) ELSE NULL END)::DOUBLE AS wr_with,
                    SUM(CASE WHEN "{col_y}" > 0 THEN 1 ELSE 0 END) AS n_with,
                    AVG(CASE WHEN "{col_y}" = 0 THEN CAST(won AS INT) ELSE NULL END)::DOUBLE AS wr_without,
                    SUM(CASE WHEN "{col_y}" = 0 THEN 1 ELSE 0 END) AS n_without
                FROM "{tname}"
                WHERE "{col_x}" > 0
            """).fetchone()

            wr_with, n_with, wr_without, n_without = res
            if not n_with or n_with < min_games:
                continue
            if wr_with is None or wr_without is None:
                continue

            delta = wr_with - wr_without
            if abs(delta) < min_delta:
                continue

            rows.append({
                "card_x": cx,
                "card_y": cy,
                "wr_with": wr_with,
                "wr_without": wr_without,
                "delta": delta,
                "n": int(n_with),
            })

    fd, tmpjson = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rows, f)
        tmp = duckdb.connect()
        tmp.execute(f"CREATE TABLE synergy AS SELECT * FROM read_json_auto('{tmpjson}')")
        tmp.execute(f"COPY synergy TO '{out}' (FORMAT PARQUET)")
        tmp.close()
    finally:
        os.unlink(tmpjson)

    logger.info("Synergy: %d pairs (|delta|>=%.2f, n>=%d) -> %s", len(rows), min_delta, min_games, out.name)
    return out
