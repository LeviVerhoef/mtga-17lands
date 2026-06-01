"""
identity/set_codes.py

Override map for set codes that differ between Scryfall and 17Lands.
Scryfall set code -> 17Lands expansion code.

Add entries here when a new set causes unresolved card identities.
"""

SCRYFALL_TO_17LANDS: dict[str, str] = {
    # Alchemy sets: Scryfall uses "y" prefix, 17Lands uses base set code
    "yone": "ONE",
    "ymom": "MOM",
    "ybro": "BRO",
    "ydmu": "DMU",
    "ysnc": "SNC",
    "yneo": "NEO",
    "yvow": "VOW",
    "ymid": "MID",
    "yafr": "AFR",
    "ystx": "STX",
    "ykhm": "KHM",
    "yznr": "ZNR",
    # Remaster sets
    "dmu": "DMU",
    # Add more overrides as discovered
}

# Reverse lookup: 17Lands expansion -> Scryfall set code(s)
_LANDS_TO_SCRYFALL: dict[str, list[str]] = {}
for scryfall_code, lands_code in SCRYFALL_TO_17LANDS.items():
    _LANDS_TO_SCRYFALL.setdefault(lands_code, []).append(scryfall_code)


def scryfall_to_17lands(set_code: str) -> str:
    """Normalize a Scryfall set code to its 17Lands equivalent."""
    return SCRYFALL_TO_17LANDS.get(set_code.lower(), set_code.upper())


def lands_to_scryfall(expansion: str) -> list[str]:
    """Return all Scryfall set codes that map to this 17Lands expansion."""
    base = expansion.upper()
    extras = _LANDS_TO_SCRYFALL.get(base, [])
    return [base.lower()] + extras
