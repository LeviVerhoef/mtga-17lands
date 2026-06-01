"""
tests/test_identity.py

Unit tests for the identity package.
These tests do NOT hit Scryfall — they use a local fixture or the cached map.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _minimal_scryfall_entry(name: str, arena_id: int, set_code: str = "blb") -> dict:
    return {
        "name": name,
        "arena_id": arena_id,
        "set": set_code,
        "colors": ["G"],
        "color_identity": ["G"],
        "mana_cost": "{2}{G}",
        "cmc": 3.0,
        "rarity": "common",
        "type_line": "Creature — Animal",
        "card_faces": [],
    }


def _mock_bulk_response(entries: list[dict]):
    """Return a mock httpx response streaming the given card list."""
    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.read.return_value = json.dumps(entries).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


@pytest.fixture
def sample_cards():
    return [
        _minimal_scryfall_entry("Lightfoot Rogue", 12345, "blb"),
        _minimal_scryfall_entry("River Serpent", 67890, "blb"),
        _minimal_scryfall_entry("Ral, Crackling Wit", 99999, "otj"),
    ]


@pytest.fixture
def scryfall_map(tmp_path, sample_cards):
    """Build a ScryfallMap backed by tmp_path, using mocked HTTP."""
    from identity import scryfall_map as sm_mod

    cache = tmp_path / "scryfall_identity.json"

    with (
        patch.object(sm_mod, "_DATA_DIR", tmp_path),
        patch.object(sm_mod, "_CACHE_PATH", cache),
        patch("identity.scryfall_map._fetch_bulk_uri", return_value="https://fake/bulk"),
        patch("httpx.Client") as mock_client_cls,
    ):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.stream.return_value = _mock_bulk_response(sample_cards)
        mock_client_cls.return_value = ctx

        from identity.scryfall_map import ScryfallMap
        yield ScryfallMap(force_refresh=True)


class TestScryfallMap:
    def test_get_by_grp_int(self, scryfall_map):
        card = scryfall_map.get_by_grp(12345)
        assert card is not None
        assert card["name"] == "Lightfoot Rogue"

    def test_get_by_grp_str(self, scryfall_map):
        card = scryfall_map.get_by_grp("67890")
        assert card["name"] == "River Serpent"

    def test_get_by_name_case_insensitive(self, scryfall_map):
        card = scryfall_map.get_by_name("lightfoot rogue")
        assert card is not None

    def test_name_for_grp_unknown(self, scryfall_map):
        assert scryfall_map.name_for_grp(0) is None

    def test_resolve_pack_logs_unknowns(self, scryfall_map):
        results = scryfall_map.resolve_pack([12345, 0])
        assert results[0]["name"] == "Lightfoot Rogue"
        assert "Unknown" in results[1]["name"]

    def test_len(self, scryfall_map):
        assert len(scryfall_map) == 3

    def test_set_17lands_normalization(self, scryfall_map):
        card = scryfall_map.get_by_grp(12345)
        # blb -> BLB (no override needed for standard sets)
        assert card["set_17lands"] == "BLB"


class TestSetCodes:
    def test_alchemy_override(self):
        from identity.set_codes import scryfall_to_17lands
        assert scryfall_to_17lands("yone") == "ONE"

    def test_standard_passthrough(self):
        from identity.set_codes import scryfall_to_17lands
        assert scryfall_to_17lands("blb") == "BLB"
