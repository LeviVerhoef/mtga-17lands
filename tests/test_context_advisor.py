"""
tests/test_context_advisor.py

Unit tests for the context advisor layer.
All tests work without real parquet artifacts by writing minimal temp files
or verifying graceful degradation when artifacts are absent.
"""

import duckdb
import pytest
from pathlib import Path
from unittest.mock import patch

from src.advisor.schema import Recommendation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rec(**kwargs) -> Recommendation:
    defaults = dict(
        card_name="Test Card",
        base_win_rate=55.0,
        contextual_score=60.0,
        z_score=0.5,
        cast_probability=1.0,
        wheel_chance=0.0,
        functional_cmc=3.0,
        reasoning=[],
        is_elite=False,
        archetype_fit="WU",
        tags=[],
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


def _write_parquet(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    """Write a list-of-dicts to a parquet file via DuckDB (via JSON intermediary)."""
    import json
    path = tmp_path / name
    json_path = tmp_path / f"{name}.json"
    json_path.write_text(json.dumps(rows))
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM read_json_auto('{json_path}')) TO '{path}' (FORMAT PARQUET)"
    )
    con.close()
    return path


# ---------------------------------------------------------------------------
# Graceful degradation — no artifacts present
# ---------------------------------------------------------------------------

class TestNoDegradation:
    def test_annotate_no_artifacts_returns_unchanged(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor
        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False
        advisor._trophy = None
        advisor._cooc = None
        advisor._synergy = None

        # Point artifact dir at empty tmp_path
        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Lightning Bolt", contextual_score=70.0)
        result = advisor.annotate([rec], pool_names=["Giant Growth"])

        assert len(result) == 1
        assert result[0].contextual_score == 70.0
        assert result[0].trophy_rate_delta is None
        assert result[0].pool_lift is None
        assert result[0].pool_synergy_delta is None

    def test_annotate_empty_pack_returns_empty(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor
        advisor = ContextAdvisor("TST", "PremierDraft")
        advisor._loaded = True
        advisor._trophy = None
        advisor._cooc = None
        advisor._synergy = None

        result = advisor.annotate([], pool_names=["Giant Growth"])
        assert result == []


# ---------------------------------------------------------------------------
# Trophy artifact
# ---------------------------------------------------------------------------

class TestTrophy:
    def test_positive_rate_delta_bumps_score(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.trophy_pick_stats.parquet", [
            {"card_name": "Bolt", "pick_rate_delta": 0.10, "ata_delta": -0.5, "seen_trophy": 200},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Bolt", contextual_score=60.0)
        result = advisor.annotate([rec], pool_names=[])

        r = result[0]
        assert r.trophy_rate_delta == pytest.approx(0.10)
        assert r.trophy_ata_delta == pytest.approx(-0.5)
        assert r.contextual_score > 60.0  # bumped

    def test_negative_rate_delta_reduces_score(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.trophy_pick_stats.parquet", [
            {"card_name": "Weak Card", "pick_rate_delta": -0.15, "ata_delta": 1.0, "seen_trophy": 300},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Weak Card", contextual_score=60.0)
        result = advisor.annotate([rec], pool_names=[])
        assert result[0].contextual_score < 60.0

    def test_low_trophy_sample_suppressed(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.trophy_pick_stats.parquet", [
            {"card_name": "Rare Card", "pick_rate_delta": 0.30, "ata_delta": -2.0, "seen_trophy": 10},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Rare Card", contextual_score=60.0)
        result = advisor.annotate([rec], pool_names=[])
        # Suppressed due to low seen_trophy
        assert result[0].trophy_rate_delta is None
        assert result[0].contextual_score == 60.0

    def test_trophy_reason_injected(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.trophy_pick_stats.parquet", [
            {"card_name": "Good Card", "pick_rate_delta": 0.12, "ata_delta": -0.8, "seen_trophy": 150},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Good Card", reasoning=["Archetype Glue (+5.0)"])
        result = advisor.annotate([rec], pool_names=[])

        reasons = result[0].reasoning
        # Trophy reason should be prepended
        assert any("Trophy" in r for r in reasons)
        assert "Archetype Glue (+5.0)" in reasons


# ---------------------------------------------------------------------------
# Co-occurrence artifact
# ---------------------------------------------------------------------------

class TestCooccurrence:
    def test_pool_lift_annotation(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.cooccurrence.trophy.parquet", [
            {"card_x": "Pack Card", "card_y": "Pool Card A", "lift": 1.8},
            {"card_x": "Pack Card", "card_y": "Pool Card B", "lift": 2.1},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Pack Card", contextual_score=50.0)
        result = advisor.annotate([rec], pool_names=["Pool Card A", "Pool Card B"])

        r = result[0]
        assert r.pool_lift == pytest.approx(1.95, abs=0.01)
        assert r.contextual_score > 50.0

    def test_lift_below_threshold_ignored(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.cooccurrence.trophy.parquet", [
            {"card_x": "Pack Card", "card_y": "Pool Card A", "lift": 1.05},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Pack Card", contextual_score=50.0)
        result = advisor.annotate([rec], pool_names=["Pool Card A"])
        assert result[0].pool_lift is None
        assert result[0].contextual_score == 50.0


# ---------------------------------------------------------------------------
# Synergy artifact
# ---------------------------------------------------------------------------

class TestSynergy:
    def test_pool_synergy_bumps_score_and_annotation(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.synergy.parquet", [
            {"card_x": "Good Card", "card_y": "Pool A", "delta": 0.04},
            {"card_x": "Good Card", "card_y": "Pool B", "delta": 0.03},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Good Card", contextual_score=55.0)
        result = advisor.annotate([rec], pool_names=["Pool A", "Pool B"])

        r = result[0]
        assert r.pool_synergy_delta == pytest.approx(0.07, abs=0.001)
        assert r.contextual_score > 55.0
        assert any("WR" in reason for reason in r.reasoning)

    def test_card_not_in_synergy_table_unchanged(self, tmp_path):
        from analysis.context_advisor import ContextAdvisor

        _write_parquet(tmp_path, "TST.PremierDraft.synergy.parquet", [
            {"card_x": "Other Card", "card_y": "Pool A", "delta": 0.10},
        ])

        advisor = ContextAdvisor.__new__(ContextAdvisor)
        advisor.expansion = "TST"
        advisor.event_type = "PremierDraft"
        advisor._loaded = False

        with patch("analysis.context_advisor._ARTIFACTS", tmp_path):
            advisor._load()

        rec = _make_rec(card_name="Unknown Card", contextual_score=55.0)
        result = advisor.annotate([rec], pool_names=["Pool A"])
        assert result[0].pool_synergy_delta is None
        assert result[0].contextual_score == 55.0


# ---------------------------------------------------------------------------
# Cache / module-level helpers
# ---------------------------------------------------------------------------

class TestCache:
    def test_get_advisor_returns_same_instance(self):
        from analysis.context_advisor import get_advisor, clear_cache
        clear_cache()
        a1 = get_advisor("BLB", "PremierDraft")
        a2 = get_advisor("BLB", "PremierDraft")
        assert a1 is a2
        clear_cache()

    def test_get_advisor_different_sets_distinct(self):
        from analysis.context_advisor import get_advisor, clear_cache
        clear_cache()
        a1 = get_advisor("BLB", "PremierDraft")
        a2 = get_advisor("DSK", "PremierDraft")
        assert a1 is not a2
        clear_cache()

    def test_clear_cache_evicts(self):
        from analysis.context_advisor import get_advisor, clear_cache
        clear_cache()
        a1 = get_advisor("BLB", "PremierDraft")
        clear_cache()
        a2 = get_advisor("BLB", "PremierDraft")
        assert a1 is not a2
        clear_cache()
