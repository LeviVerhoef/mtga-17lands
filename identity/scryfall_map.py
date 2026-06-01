"""
identity/scryfall_map.py

Builds and persists a grpId (Arena ID) -> card info lookup from Scryfall's
bulk default_cards data. Everything the analysis engine needs to join grpIds
to names and metadata.

Usage:
    from identity.scryfall_map import ScryfallMap
    m = ScryfallMap()
    card = m.get_by_grp(12345)   # -> {"name": ..., "set": ..., ...}
    card = m.get_by_name("Lightning Bolt")
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from identity.set_codes import scryfall_to_17lands

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_CACHE_PATH = _DATA_DIR / "scryfall_identity.json"
_BULK_API = "https://api.scryfall.com/bulk-data"
_HEADERS = {
    "User-Agent": "mtga-17lands/1.0 (draft overlay; github.com/LeviVerhoef/mtga-17lands)",
    "Accept": "application/json",
}
_MAX_AGE_DAYS = 7


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age_days = (time.time() - _CACHE_PATH.stat().st_mtime) / 86400
    return age_days < _MAX_AGE_DAYS


def _fetch_bulk_uri() -> str:
    with httpx.Client(headers=_HEADERS, timeout=30) as client:
        resp = client.get(_BULK_API)
        resp.raise_for_status()
        for item in resp.json()["data"]:
            if item["type"] == "default_cards":
                return item["download_uri"]
    raise RuntimeError("default_cards bulk data not found in Scryfall bulk-data index")


def _build_map(download_uri: str) -> dict:
    """Download default_cards and build grpId + name indexes."""
    logger.info("Downloading Scryfall default_cards bulk data...")
    grp_index: dict[str, dict] = {}
    name_index: dict[str, dict] = {}
    unresolved: list[int] = []

    with httpx.Client(headers=_HEADERS, timeout=120) as client:
        with client.stream("GET", download_uri) as resp:
            resp.raise_for_status()
            cards = json.loads(resp.read())

    for card in cards:
        arena_id = card.get("arena_id")
        name = card.get("name", "")
        set_code = card.get("set", "")
        entry = {
            "name": name,
            "set_scryfall": set_code,
            "set_17lands": scryfall_to_17lands(set_code),
            "colors": card.get("colors", []),
            "color_identity": card.get("color_identity", []),
            "mana_cost": card.get("mana_cost", ""),
            "cmc": card.get("cmc", 0),
            "rarity": card.get("rarity", ""),
            "type_line": card.get("type_line", ""),
        }
        if arena_id:
            grp_index[str(arena_id)] = entry
        name_index[name.lower()] = entry

        # Also index by card_faces names for double-faced cards
        for face in card.get("card_faces", []):
            face_name = face.get("name", "")
            if face_name:
                name_index[face_name.lower()] = entry

    logger.info(
        "Scryfall map built: %d arena_id entries, %d name entries",
        len(grp_index),
        len(name_index),
    )
    return {"grp": grp_index, "name": name_index, "unresolved": unresolved}


def _load_or_build() -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _cache_is_fresh():
        logger.debug("Loading Scryfall identity map from cache")
        with _CACHE_PATH.open(encoding="utf-8") as f:
            return json.load(f)

    uri = _fetch_bulk_uri()
    data = _build_map(uri)
    with _CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    logger.info("Scryfall identity map saved to %s", _CACHE_PATH)
    return data


class ScryfallMap:
    """Thread-safe singleton-like identity map. Construct once; reuse."""

    def __init__(self, force_refresh: bool = False):
        if force_refresh and _CACHE_PATH.exists():
            _CACHE_PATH.unlink()
        data = _load_or_build()
        self._grp: dict[str, dict] = data["grp"]
        self._name: dict[str, dict] = data["name"]

    def get_by_grp(self, grp_id: int | str) -> Optional[dict]:
        return self._grp.get(str(grp_id))

    def get_by_name(self, name: str) -> Optional[dict]:
        return self._name.get(name.lower())

    def name_for_grp(self, grp_id: int | str) -> Optional[str]:
        entry = self.get_by_grp(grp_id)
        return entry["name"] if entry else None

    def resolve_pack(self, grp_ids: list[int | str]) -> list[dict]:
        """Resolve a list of grpIds to card info dicts. Logs any misses."""
        results = []
        for gid in grp_ids:
            entry = self.get_by_grp(gid)
            if entry:
                results.append({"grp_id": int(gid), **entry})
            else:
                logger.warning("Unresolved Arena grpId: %s", gid)
                results.append({"grp_id": int(gid), "name": f"[Unknown:{gid}]"})
        return results

    def __len__(self) -> int:
        return len(self._grp)
