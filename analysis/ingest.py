"""
analysis/ingest.py

Downloads 17Lands bulk datasets and loads them into DuckDB.
Queries .csv.gz directly — never loads full dataset into RAM.

Usage:
    from analysis.ingest import DatasetIngestor
    ing = DatasetIngestor()
    ing.ensure_dataset("BLB", "PremierDraft", "draft_data")
    con = ing.connection("BLB", "PremierDraft")
"""

import logging
import os
from pathlib import Path
from typing import Literal

import duckdb
import httpx

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_S3_BASE = "https://17lands-public.s3.amazonaws.com/analysis_data"
_HEADERS = {
    "User-Agent": "mtga-17lands/1.0 (draft overlay; github.com/LeviVerhoef/mtga-17lands)"
}

DatasetKind = Literal["draft_data", "game_data"]
_FILE_NAMES = {
    "draft_data": "draft_data_public",
    "game_data": "game_data_public",
}


def _s3_url(kind: DatasetKind, expansion: str, event_type: str) -> str:
    prefix = _FILE_NAMES[kind]
    return f"{_S3_BASE}/{kind}/{prefix}.{expansion}.{event_type}.csv.gz"


def _local_gz(kind: DatasetKind, expansion: str, event_type: str) -> Path:
    return _DATA_DIR / "bulk" / f"{kind}.{expansion}.{event_type}.csv.gz"


def _db_path(expansion: str, event_type: str) -> Path:
    return _DATA_DIR / "duckdb" / f"{expansion}.{event_type}.duckdb"


class DatasetIngestor:
    def __init__(self):
        (_DATA_DIR / "bulk").mkdir(parents=True, exist_ok=True)
        (_DATA_DIR / "duckdb").mkdir(parents=True, exist_ok=True)
        self._connections: dict[str, duckdb.DuckDBPyConnection] = {}

    def ensure_dataset(
        self,
        expansion: str,
        event_type: str,
        kind: DatasetKind,
        force: bool = False,
    ) -> Path:
        """Download the .csv.gz if not already present (or force=True)."""
        dest = _local_gz(kind, expansion, event_type)
        if dest.exists() and not force:
            logger.info("%s already downloaded, skipping", dest.name)
            return dest

        url = _s3_url(kind, expansion, event_type)
        logger.info("Downloading %s -> %s", url, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        with httpx.stream("GET", url, headers=_HEADERS, timeout=600, follow_redirects=True) as resp:
            if resp.status_code == 404:
                raise FileNotFoundError(
                    f"Dataset not found on S3: {url}\n"
                    "Check https://www.17lands.com/public_datasets for available sets."
                )
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)

        logger.info("Downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest

    def connection(self, expansion: str, event_type: str) -> duckdb.DuckDBPyConnection:
        """Return (or open) the persistent DuckDB connection for this set."""
        key = f"{expansion}.{event_type}"
        if key not in self._connections:
            db = _db_path(expansion, event_type)
            db.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(str(db))
            self._tune(con)
            self._connections[key] = con
        return self._connections[key]

    @staticmethod
    def _tune(con: duckdb.DuckDBPyConnection) -> None:
        """
        Bound memory and allow on-disk spilling so the large set-based
        aggregations (co-occurrence / synergy self-joins over millions of
        rows) complete on a laptop instead of OOM-ing.
        """
        spill = _DATA_DIR / "duckdb" / "tmp"
        spill.mkdir(parents=True, exist_ok=True)
        mem = os.environ.get("MTGA_DUCKDB_MEMORY_LIMIT", "6GB")
        try:
            con.execute(f"SET memory_limit='{mem}'")
            con.execute(f"SET temp_directory='{spill}'")
            con.execute("SET preserve_insertion_order=false")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("DuckDB tuning skipped: %s", exc)

    def load_into_db(
        self,
        expansion: str,
        event_type: str,
        kind: DatasetKind,
        table_name: str | None = None,
    ) -> str:
        """
        Register the .csv.gz as a DuckDB table (or view).
        Returns the table name.
        """
        gz = _local_gz(kind, expansion, event_type)
        if not gz.exists():
            raise FileNotFoundError(
                f"{gz} not found. Call ensure_dataset() first."
            )

        tname = table_name or kind
        con = self.connection(expansion, event_type)

        existing = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", [tname]
        ).fetchone()
        if existing:
            logger.debug("Table %s already exists in DuckDB, skipping load", tname)
            return tname

        logger.info("Loading %s into DuckDB table '%s'...", gz.name, tname)
        # ignore_errors: some 17Lands published files contain a trailing garbage
        # row (e.g. a line of null bytes in AFR). Skip such rows rather than abort
        # the whole load; legitimate data is unaffected.
        con.execute(f"""
            CREATE TABLE "{tname}" AS
            SELECT * FROM read_csv_auto('{gz}', compression='gzip', header=True,
                                        ignore_errors=true)
        """)
        count = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
        logger.info("Loaded %d rows into '%s'", count, tname)
        return tname

    def dump_headers(self, expansion: str, event_type: str, kind: DatasetKind) -> list[str]:
        """Return the column names from the .csv.gz without loading the full file."""
        gz = _local_gz(kind, expansion, event_type)
        if not gz.exists():
            raise FileNotFoundError(f"{gz} not found. Call ensure_dataset() first.")
        con = duckdb.connect()
        result = con.execute(
            f"SELECT * FROM read_csv_auto('{gz}', compression='gzip', header=True) LIMIT 0"
        )
        return [desc[0] for desc in result.description]

    def close_all(self):
        for con in self._connections.values():
            con.close()
        self._connections.clear()
