"""
analysis/similar_pools.py

"Pools like yours" — k-NN over historical draft pool vectors.

Given the current pool (sparse vector over cards), find similar historical
pools and aggregate what those drafters picked next.

Output artifacts:
  data/artifacts/<SET>.<FORMAT>.pool_vectors.parquet   (pool_id, card_name, count)
  data/artifacts/<SET>.<FORMAT>.pool_picks.parquet     (pool_id, pack, pick, card_picked, cards_available...)

Query at runtime via SimilarPools.recommend().
"""

import logging
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np

from analysis.ingest import DatasetIngestor

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"
_MIN_SAMPLES = 500       # minimum pools to index before recommending
_TOP_K = 50              # neighbors to aggregate over
_MAX_PICK_ROWS = 200_000  # cap indexed pick states so artifacts stay compact


def build(
    expansion: str,
    event_type: str,
    trophy_only: bool = False,
    ingestor: DatasetIngestor | None = None,
    max_pick_rows: int = _MAX_PICK_ROWS,
) -> tuple[Path, Path]:
    """
    Build and persist pool vector index and pick tables.
    Returns (pool_vectors path, pool_picks path).

    Both artifacts are produced with set-based SQL (UNPIVOT) rather than a
    Python row loop — on a full set draft_data has ~6M pick rows, and the old
    iterrows() approach exploded to 100M+ dict rows and OOM-ed. Pick states are
    reservoir-sampled down to ``max_pick_rows`` so the parquet stays compact and
    the k-NN matrix loads in memory at query time.
    """
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    suffix = "trophy" if trophy_only else "all"
    vec_out = _ARTIFACTS / f"{expansion}.{event_type}.pool_vectors.{suffix}.parquet"
    picks_out = _ARTIFACTS / f"{expansion}.{event_type}.pool_picks.{suffix}.parquet"

    ing = ingestor or DatasetIngestor()
    tname = ing.load_into_db(expansion, event_type, "draft_data")
    con = ing.connection(expansion, event_type)

    cols = [row[0] for row in con.execute(f'DESCRIBE "{tname}"').fetchall()]
    pool_cols = [c for c in cols if c.startswith("pool_")]
    pack_cols = [c for c in cols if c.startswith("pack_card_")]
    if not pool_cols or not pack_cols:
        raise ValueError(f"Missing pool_* or pack_card_* columns in {tname}.")

    where = "WHERE event_match_wins = 7" if trophy_only else ""
    pool_col_list = ", ".join(f'"{c}"' for c in pool_cols)
    pack_col_list = ", ".join(f'"{c}"' for c in pack_cols)
    pool_prefix = len("pool_")
    pack_prefix = len("pack_card_")

    logger.info("Extracting pool/pick rows from %s.%s (%s)...", expansion, event_type, suffix)

    # Only index picks where the drafter already has a pool to compare against
    # (pick_number+pack>0). Reservoir-sample to keep the artifact bounded.
    total = con.execute(f"""
        SELECT COUNT(*) FROM "{tname}"
        {where}{' AND' if where else 'WHERE'} (pack_number * 15 + pick_number) > 0
    """).fetchone()[0]
    sample_clause = f"USING SAMPLE {max_pick_rows} ROWS" if total > max_pick_rows else ""

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _picks AS
        SELECT
            draft_id || '_' || pack_number || '_' || pick_number AS pick_id,
            draft_id, pack_number, pick_number, pick,
            {pool_col_list}, {pack_col_list}
        FROM "{tname}"
        {where}{' AND' if where else 'WHERE'} (pack_number * 15 + pick_number) > 0
        {sample_clause}
    """)

    if total < _MIN_SAMPLES:
        logger.warning("Only %d pick rows — too few for reliable recommendations", total)

    # pool_vectors (long): pick_id, card_name, count — one row per card in pool.
    con.execute(f"""
        COPY (
            SELECT pick_id, substr(name, {pool_prefix + 1}) AS card_name, cnt AS count
            FROM (UNPIVOT _picks ON {pool_col_list} INTO NAME name VALUE cnt)
            WHERE cnt > 0
        ) TO '{vec_out}' (FORMAT PARQUET)
    """)

    # pool_picks: pick_id, draft_id, pack, pick, card_picked, cards_available[].
    con.execute(f"""
        COPY (
            SELECT
                pick_id,
                any_value(draft_id) AS draft_id,
                any_value(pack_number) AS pack_number,
                any_value(pick_number) AS pick_number,
                any_value(pick) AS card_picked,
                list(card) AS cards_available
            FROM (
                SELECT pick_id, draft_id, pack_number, pick_number, pick,
                       substr(name, {pack_prefix + 1}) AS card
                FROM (UNPIVOT _picks ON {pack_col_list} INTO NAME name VALUE cnt)
                WHERE cnt > 0
            )
            GROUP BY pick_id
        ) TO '{picks_out}' (FORMAT PARQUET)
    """)

    logger.info("Pool vectors: %s | Pool picks: %s", vec_out.name, picks_out.name)
    return vec_out, picks_out


class SimilarPools:
    """
    Runtime query engine: given a current pool, recommend cards from the pack.
    Load once per session; call recommend() per pick.
    """

    def __init__(self, expansion: str, event_type: str, trophy_only: bool = False):
        suffix = "trophy" if trophy_only else "all"
        vec_path = _ARTIFACTS / f"{expansion}.{event_type}.pool_vectors.{suffix}.parquet"
        picks_path = _ARTIFACTS / f"{expansion}.{event_type}.pool_picks.{suffix}.parquet"

        if not vec_path.exists() or not picks_path.exists():
            raise FileNotFoundError(
                f"Artifacts not found for {expansion}.{event_type}. Run analysis.similar_pools.build() first."
            )

        self._con = duckdb.connect()
        self._con.execute(f"CREATE TABLE pool_vectors AS SELECT * FROM read_parquet('{vec_path}')")
        self._con.execute(f"CREATE TABLE pool_picks AS SELECT * FROM read_parquet('{picks_path}')")

        # Build card vocabulary from pool vectors
        cards = self._con.execute(
            "SELECT DISTINCT card_name FROM pool_vectors ORDER BY card_name"
        ).fetchall()
        self._vocab = {row[0]: i for i, row in enumerate(cards)}
        self._vocab_list = [row[0] for row in cards]

        # Load all pool vectors as a matrix for k-NN
        logger.info("Loading pool vector matrix (%d cards)...", len(self._vocab))
        self._pick_ids, self._matrix = self._build_matrix()

    def _build_matrix(self) -> tuple[list[str], np.ndarray]:
        rows = self._con.execute(
            "SELECT pick_id, card_name, count FROM pool_vectors"
        ).fetchall()
        # Group by pick_id
        id_to_vec: dict[str, np.ndarray] = {}
        for pick_id, card_name, count in rows:
            if card_name not in self._vocab:
                continue
            if pick_id not in id_to_vec:
                id_to_vec[pick_id] = np.zeros(len(self._vocab), dtype=np.float32)
            id_to_vec[pick_id][self._vocab[card_name]] = count

        pick_ids = list(id_to_vec.keys())
        matrix = np.stack([id_to_vec[pid] for pid in pick_ids])
        # L2-normalize for cosine similarity
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        matrix /= norms
        return pick_ids, matrix

    def recommend(
        self,
        current_pool: dict[str, int],
        available_cards: list[str],
        k: int = _TOP_K,
    ) -> list[dict]:
        """
        Given the current pool (card_name -> count) and cards available in the
        current pack, return a ranked list of available cards by how often
        similar historical pools picked them.

        Returns: [{"card": ..., "pick_rate": ..., "n": ...}, ...]
        """
        if not self._pick_ids:
            return [{"card": c, "pick_rate": None, "n": 0} for c in available_cards]

        # Build query vector
        q = np.zeros(len(self._vocab), dtype=np.float32)
        for card, count in current_pool.items():
            if card in self._vocab:
                q[self._vocab[card]] = count
        norm = np.linalg.norm(q)
        if norm > 0:
            q /= norm

        # Cosine similarity to all historical pool states
        sims = self._matrix @ q
        top_k_idx = np.argpartition(sims, -min(k, len(sims)))[-min(k, len(sims)):]
        neighbor_ids = [self._pick_ids[i] for i in top_k_idx]

        # Count how often each available card was picked by neighbors
        neighbor_list = "', '".join(neighbor_ids)
        picks = self._con.execute(f"""
            SELECT card_picked, COUNT(*) AS n
            FROM pool_picks
            WHERE pick_id IN ('{neighbor_list}')
            GROUP BY card_picked
        """).fetchall()
        total = sum(p[1] for p in picks)
        pick_counts = {p[0]: p[1] for p in picks}

        result = []
        for card in available_cards:
            n = pick_counts.get(card, 0)
            result.append({
                "card": card,
                "pick_rate": n / total if total > 0 else None,
                "n": n,
            })
        result.sort(key=lambda x: x["pick_rate"] or 0, reverse=True)
        return result
