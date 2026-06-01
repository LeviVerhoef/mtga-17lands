"""
analysis/cooccurrence.py

Computes card co-occurrence within final draft pools.
"What else do trophy drafters take with this card?"

For each card X in trophy (and all) pools:
  P(pool contains Y | pool contains X)
  lift = P(Y|X) / P(Y)

Output (data/artifacts/<SET>.<FORMAT>.cooccurrence.parquet):
  card_x, card_y, co_count, p_y_given_x, p_y, lift, pool_count
  (rows only where both p_y_given_x and lift meet minimums)
"""

import logging
from pathlib import Path

import duckdb

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_MIN_SUPPORT = 20   # minimum co-occurrence count
_MIN_LIFT = 1.1     # only store pairs with meaningful lift


def compute(
    expansion: str,
    event_type: str,
    trophy_only: bool = False,
    ingestor: DatasetIngestor | None = None,
) -> Path:
    """Compute and save co-occurrence parquet. Returns output path."""
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    suffix = "trophy" if trophy_only else "all"
    out = _ARTIFACTS / f"{expansion}.{event_type}.cooccurrence.{suffix}.parquet"

    ing = ingestor or DatasetIngestor()
    tname = ing.load_into_db(expansion, event_type, "draft_data")
    con = ing.connection(expansion, event_type)

    cols = [row[0] for row in con.execute(f'DESCRIBE "{tname}"').fetchall()]
    pool_cols = [c for c in cols if c.startswith("pool_")]
    if not pool_cols:
        raise ValueError(f"No pool_* columns in {tname}.")

    where = "WHERE event_match_wins = 7" if trophy_only else ""

    # Build a per-draft-id final pool snapshot.
    # draft_data has one row per pick; we want the state at the last pick of
    # each draft (max pick_number within each draft_id = the final pool row).
    # pool_<Name> columns represent cumulative pool size up to that pick.
    pool_col_sql = ", ".join(f'"{c}"' for c in pool_cols)

    # Get total pool count first
    pool_count = con.execute(f"""
        SELECT COUNT(DISTINCT draft_id)
        FROM "{tname}"
        {where}
    """).fetchone()[0]

    logger.info(
        "Computing co-occurrence over %d drafts (%s)...", pool_count, suffix
    )

    # For efficiency, compute using DuckDB SQL directly over the bulk table.
    # We'll write the co-occurrence pairs to a temp parquet, then filter.
    card_names = [c[len("pool_"):] for c in pool_cols]

    # Marginal probabilities: P(card in final pool)
    # Use last-pick rows per draft
    marginals_sql = f"""
        WITH last_picks AS (
            SELECT draft_id, MAX(pick_number + pack_number * 15) AS last_seq
            FROM "{tname}"
            {where}
            GROUP BY draft_id
        ),
        final_pools AS (
            SELECT t.*
            FROM "{tname}" t
            JOIN last_picks lp
              ON t.draft_id = lp.draft_id
             AND (t.pick_number + t.pack_number * 15) = lp.last_seq
            {where.replace("WHERE", "AND") if where else ""}
        )
        SELECT
            {", ".join(f'SUM(CASE WHEN "{c}" > 0 THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) AS p_{i}' for i, c in enumerate(pool_cols))}
        FROM final_pools
    """

    marginals_row = con.execute(marginals_sql).fetchone()
    marginals = {card_names[i]: marginals_row[i] for i in range(len(card_names))}

    rows = []
    for i, cx in enumerate(card_names):
        p_x = marginals[cx]
        if p_x == 0:
            continue
        col_x = pool_cols[i]

        for j, cy in enumerate(card_names):
            if i == j:
                continue
            p_y = marginals[cy]
            if p_y == 0:
                continue
            col_y = pool_cols[j]

            co_sql = f"""
                WITH last_picks AS (
                    SELECT draft_id, MAX(pick_number + pack_number * 15) AS last_seq
                    FROM "{tname}"
                    {where}
                    GROUP BY draft_id
                ),
                final_pools AS (
                    SELECT t.*
                    FROM "{tname}" t
                    JOIN last_picks lp
                      ON t.draft_id = lp.draft_id
                     AND (t.pick_number + t.pack_number * 15) = lp.last_seq
                    {where.replace("WHERE", "AND") if where else ""}
                )
                SELECT
                    SUM(CASE WHEN "{col_x}" > 0 AND "{col_y}" > 0 THEN 1 ELSE 0 END) AS co
                FROM final_pools
            """
            co_count = con.execute(co_sql).fetchone()[0] or 0
            if co_count < _MIN_SUPPORT:
                continue
            p_y_given_x = co_count / (p_x * pool_count) if p_x * pool_count > 0 else 0
            lift = p_y_given_x / p_y if p_y > 0 else 0
            if lift < _MIN_LIFT:
                continue
            rows.append({
                "card_x": cx,
                "card_y": cy,
                "co_count": co_count,
                "p_y_given_x": p_y_given_x,
                "p_y": p_y,
                "lift": lift,
                "pool_count": pool_count,
            })

    tmp = duckdb.connect()
    tmp.execute("CREATE TABLE cooc AS SELECT * FROM rows")
    tmp.execute(f"COPY cooc TO '{out}' (FORMAT PARQUET)")
    tmp.close()

    logger.info("Co-occurrence: %d pairs (lift>%.1f, support>%d) -> %s", len(rows), _MIN_LIFT, _MIN_SUPPORT, out.name)
    return out
