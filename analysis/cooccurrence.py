"""
analysis/cooccurrence.py

Computes card co-occurrence within final draft pools.
"What else do trophy drafters take with this card?"

For each card X in trophy (and all) pools:
  P(pool contains Y | pool contains X)
  lift = P(Y|X) / P(Y)

Output (data/artifacts/<SET>.<FORMAT>.cooccurrence.<suffix>.parquet):
  card_x, card_y, co_count, p_y_given_x, p_y, lift, pool_count
  (rows only where co_count >= _MIN_SUPPORT and lift >= _MIN_LIFT)

Implementation note: this runs as a small number of set-based SQL passes
(UNPIVOT each pool into a long present(draft_id, card) table, then a single
self-join + GROUP BY to count all pairs at once) rather than one query per
card pair. On a full set (~150k pools, 276 cards) the per-pair approach would
issue ~76k full-table scans and never finish; the set-based version is a few
passes over a 7M-row long table.
"""

import logging
from pathlib import Path

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
    min_support: int = _MIN_SUPPORT,
    min_lift: float = _MIN_LIFT,
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
    pool_col_list = ", ".join(f'"{c}"' for c in pool_cols)
    prefix_len = len("pool_")

    # 1. Final pool per draft = the last-pick row (max pack*15 + pick).
    #    Keep only draft_id + the pool_* columns to stay lean.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _final_pools AS
        WITH last_picks AS (
            SELECT draft_id, MAX(pick_number + pack_number * 15) AS last_seq
            FROM "{tname}"
            {where}
            GROUP BY draft_id
        )
        SELECT t.draft_id, {pool_col_list}
        FROM "{tname}" t
        JOIN last_picks lp
          ON t.draft_id = lp.draft_id
         AND (t.pick_number + t.pack_number * 15) = lp.last_seq
    """)

    pool_count = con.execute("SELECT COUNT(DISTINCT draft_id) FROM _final_pools").fetchone()[0]
    if not pool_count:
        logger.warning("No pools for %s.%s (%s) — empty artifact", expansion, event_type, suffix)
        con.execute("CREATE OR REPLACE TEMP TABLE _cooc AS SELECT NULL AS card_x WHERE 1=0")
        con.execute(f"COPY (SELECT * FROM _cooc) TO '{out}' (FORMAT PARQUET)")
        return out

    logger.info("Computing co-occurrence over %d pools (%s)...", pool_count, suffix)

    # 2. Long form: one row per (draft_id, card) the pool contains.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _present AS
        SELECT draft_id, substr(name, {prefix_len + 1}) AS card
        FROM (
            UNPIVOT _final_pools
            ON {pool_col_list}
            INTO NAME name VALUE cnt
        )
        WHERE cnt > 0
    """)

    # 3. Per-card marginal counts (how many pools contain each card).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _marg AS
        SELECT card, COUNT(*) AS n FROM _present GROUP BY card
    """)

    # 4. All co-occurring pairs in one self-join + aggregate, then derive
    #    lift = co * pool_count / (n_x * n_y). Filter on support and lift.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _cooc AS
        SELECT
            a.card AS card_x,
            b.card AS card_y,
            COUNT(*) AS co_count,
            COUNT(*)::DOUBLE / mx.n          AS p_y_given_x,
            my.n::DOUBLE / {pool_count}      AS p_y,
            (COUNT(*)::DOUBLE * {pool_count}) / (mx.n * my.n) AS lift,
            {pool_count} AS pool_count
        FROM _present a
        JOIN _present b
          ON a.draft_id = b.draft_id AND a.card <> b.card
        JOIN _marg mx ON mx.card = a.card
        JOIN _marg my ON my.card = b.card
        GROUP BY a.card, b.card, mx.n, my.n
        HAVING COUNT(*) >= {min_support}
           AND (COUNT(*)::DOUBLE * {pool_count}) / (mx.n * my.n) >= {min_lift}
    """)

    n_pairs = con.execute("SELECT COUNT(*) FROM _cooc").fetchone()[0]
    con.execute(f"COPY (SELECT * FROM _cooc) TO '{out}' (FORMAT PARQUET)")

    logger.info(
        "Co-occurrence: %d pairs (lift>=%.1f, support>=%d) -> %s",
        n_pairs, min_lift, min_support, out.name,
    )
    return out
