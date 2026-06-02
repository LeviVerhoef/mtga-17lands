"""
Tests for analysis.card_metrics — per-card win-rate / ALSA / ATA from bulk data.

Builds tiny in-memory game_data + draft_data tables with known values and checks
the computed metrics exactly (definitions verified against the live 17Lands
card_ratings during development).
"""

import duckdb
import pytest

import analysis.card_metrics as cm
from analysis.ingest import DatasetIngestor


class _FakeIngestor(DatasetIngestor):
    """Returns a pre-loaded in-memory connection; load is a no-op."""

    def __init__(self, con):
        self._con = con

    def load_into_db(self, expansion, event_type, kind, table_name=None):
        return kind

    def connection(self, expansion, event_type):
        return self._con


@pytest.fixture
def con():
    c = duckdb.connect()
    # Two cards: Aaa, Bbb.  game_data: 3 games.
    # g1 won: Aaa in opening hand; Bbb drawn.
    # g2 lost: Aaa drawn.
    # g3 won: Aaa in deck only (never drawn); Bbb in opening hand.
    c.execute("""
        CREATE TABLE game_data AS SELECT * FROM (VALUES
            (1, 1, 1, 0, 1, 0, 1),
            (0, 1, 0, 1, 0, 0, 0),
            (1, 1, 0, 0, 1, 1, 0)
        ) AS t(won,
               "deck_Aaa","opening_hand_Aaa","drawn_Aaa",
               "deck_Bbb","opening_hand_Bbb","drawn_Bbb")
    """)
    # draft_data: pick rows (pick_number is 0-indexed like Arena).
    c.execute("""
        CREATE TABLE draft_data AS SELECT * FROM (VALUES
            ('Aaa', 0, 1, 0),
            ('Aaa', 2, 1, 1),
            ('Bbb', 4, 0, 1)
        ) AS t(pick, pick_number, "pack_card_Aaa", "pack_card_Bbb")
    """)
    return c


def _metrics(con, tmp_path):
    cm._ARTIFACTS = tmp_path
    path = cm.compute("TST", "PremierDraft", ingestor=_FakeIngestor(con))
    rows = duckdb.connect().execute(
        f"SELECT card_name, gihwr, gih_count, gpwr, gp_count, ohwr, gdwr, alsa, ata "
        f"FROM read_parquet('{path}')"
    ).fetchall()
    return {r[0]: r for r in rows}


def test_gihwr_and_counts(con, tmp_path):
    m = _metrics(con, tmp_path)
    aaa = m["Aaa"]
    # GIH = games where Aaa in opening hand OR drawn = g1 (oh) + g2 (drawn) = 2; wins = g1 only = 1
    assert aaa[2] == 2          # gih_count
    assert aaa[1] == pytest.approx(0.5)   # gihwr
    # GP = deck>0 in all 3 games; wins = g1,g3 = 2
    assert aaa[4] == 3          # gp_count
    assert aaa[3] == pytest.approx(2 / 3)  # gpwr


def test_oh_and_gd_split(con, tmp_path):
    m = _metrics(con, tmp_path)
    aaa = m["Aaa"]
    # OH = g1 only (won) -> 1.0 ; GD = g2 only (lost) -> 0.0
    assert aaa[5] == pytest.approx(1.0)   # ohwr
    assert aaa[6] == pytest.approx(0.0)   # gdwr


def test_ata_alsa_one_indexed(con, tmp_path):
    m = _metrics(con, tmp_path)
    aaa = m["Aaa"]
    # ATA = avg(pick_number+1) where pick=='Aaa' = avg(1, 3) = 2.0
    assert aaa[8] == pytest.approx(2.0)
    # ALSA = count-weighted avg(pick_number+1) over pack appearances of Aaa
    #      = ((0+1)*1 + (2+1)*1) / 2 = 2.0
    assert aaa[7] == pytest.approx(2.0)


def test_bbb_present(con, tmp_path):
    m = _metrics(con, tmp_path)
    assert "Bbb" in m
    # Bbb GIH = g1 (drawn) + g3 (oh) = 2; wins = both won = 2 -> 1.0
    assert m["Bbb"][1] == pytest.approx(1.0)
