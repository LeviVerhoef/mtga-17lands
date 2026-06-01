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
_MIN_SAMPLES = 500   # minimum pools to index before recommending
_TOP_K = 50          # neighbors to aggregate over


def build(
    expansion: str,
    event_type: str,
    trophy_only: bool = False,
    ingestor: DatasetIngestor | None = None,
) -> tuple[Path, Path]:
    """
    Build and persist pool vector index and pick tables.
    Returns (pool_vectors path, pool_picks path).
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

    pool_col_sql = ", ".join(f'"{c}"' for c in pool_cols)
    pack_col_sql = ", ".join(f'"{c}"' for c in pack_cols)

    # Extract one row per pick with pool state + pack state + pick made
    logger.info("Extracting pool/pick rows from %s.%s (%s)...", expansion, event_type, suffix)
    rows_df = con.execute(f"""
        SELECT draft_id, pack_number, pick_number, pick,
               {pool_col_sql},
               {pack_col_sql}
        FROM "{tname}"
        {where}
    """).df()

    if len(rows_df) < _MIN_SAMPLES:
        logger.warning("Only %d rows — too few for reliable recommendations", len(rows_df))

    # Pool vectors: for each pick row, the pool_ columns form the vector
    pool_vec_cols = pool_cols
    card_names_pool = [c[len("pool_"):] for c in pool_vec_cols]

    # Write pool_vectors (long format: draft_id + pick_seq + card + count)
    tmp = duckdb.connect()
    tmp.register("raw", rows_df)

    # Build a unique pick_id
    pool_vec_rows = []
    for _, row in rows_df.iterrows():
        pick_id = f"{row['draft_id']}_{row['pack_number']}_{row['pick_number']}"
        for c, name in zip(pool_vec_cols, card_names_pool):
            v = row.get(c, 0)
            if v and v > 0:
                pool_vec_rows.append({"pick_id": pick_id, "card_name": name, "count": int(v)})

    tmp.execute("CREATE TABLE pool_vectors AS SELECT * FROM pool_vec_rows")
    tmp.execute(f"COPY pool_vectors TO '{vec_out}' (FORMAT PARQUET)")

    # Build pool_picks: pick_id, draft_id, pack, pick_num, card_picked, available cards
    pick_rows = []
    pack_card_names = [c[len("pack_card_"):] for c in pack_cols]
    for _, row in rows_df.iterrows():
        pick_id = f"{row['draft_id']}_{row['pack_number']}_{row['pick_number']}"
        available = [name for c, name in zip(pack_cols, pack_card_names) if row.get(c, 0) > 0]
        pick_rows.append({
            "pick_id": pick_id,
            "draft_id": row["draft_id"],
            "pack_number": row["pack_number"],
            "pick_number": row["pick_number"],
            "card_picked": row["pick"],
            "cards_available": available,
        })

    tmp.execute("CREATE TABLE pool_picks AS SELECT * FROM pick_rows")
    tmp.execute(f"COPY pool_picks TO '{picks_out}' (FORMAT PARQUET)")
    tmp.close()

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
