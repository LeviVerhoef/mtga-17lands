import numpy as np
import re
from src.card_logic import get_functional_cmc

try:
    from numba import njit
except ImportError:  # numba is optional — pure-numpy fallback is ~5× slower but correct
    def njit(*args, **kwargs):
        """No-op decorator used when numba is not installed."""
        def decorator(fn):
            return fn
        return decorator if args and callable(args[0]) else decorator

# Mana Bitmask Mapping
COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16}


def _parse_deck_to_arrays(deck_list):
    """Converts the deck from slow Python dicts to fast NumPy arrays."""
    flat_deck = []
    for c in deck_list:
        flat_deck.extend([c] * int(c.get("count", 1)))

    if len(flat_deck) < 40:
        return None

    is_land = np.zeros(40, dtype=np.bool_)
    is_ramp = np.zeros(40, dtype=np.bool_)
    is_removal = np.zeros(40, dtype=np.bool_)
    cmcs = np.zeros(40, dtype=np.int8)
    mana_produced = np.zeros(40, dtype=np.int32)
    primary_req = np.zeros(40, dtype=np.int32)

    for i, c in enumerate(flat_deck[:40]):
        types = c.get("types", [])
        tags = c.get("tags", [])
        text = str(c.get("oracle_text", c.get("text", ""))).lower()

        is_land[i] = "Land" in types
        is_ramp[i] = (
            "fixing_ramp" in tags or "any color" in text or "treasure" in text
        ) and not is_land[i]
        is_removal[i] = "removal" in tags
        cmcs[i] = get_functional_cmc(c)

        # Calculate produced mana bitmask
        if is_land[i] or is_ramp[i]:
            if "any color" in text or "fixing_ramp" in tags:
                mana_produced[i] = 31  # 1+2+4+8+16 (WUBRG)
            else:
                mask = 0
                for color in c.get("colors", []):
                    mask |= COLOR_BITS.get(color, 0)
                mana_produced[i] = mask

        # Calculate primary pip requirement bitmask
        if not is_land[i]:
            cost = c.get("mana_cost", "")
            mask = 0
            matches = re.findall(r"\{(.*?)\}", cost)
            for pip in matches:
                opts = [opt for opt in pip.split("/") if opt in COLOR_BITS]
                if opts:
                    mask |= COLOR_BITS[opts[0]]
            primary_req[i] = mask

    return is_land, is_ramp, is_removal, cmcs, mana_produced, primary_req


@njit(cache=True)
def _run_fast_monte_carlo(
    is_land, is_ramp, is_removal, cmcs, mana_produced, primary_req, iterations
):
    mulligans = 0
    screw_t3 = 0
    screw_t4 = 0
    flood_t5 = 0
    cast_t2 = 0
    cast_t3 = 0
    cast_t4 = 0
    curve_out = 0
    removal_t4 = 0
    color_screw_t3 = 0
    total_kept_cards = 0

    deck_indices = np.arange(40)

    for _ in range(iterations):
        np.random.shuffle(deck_indices)

        mull_count = 0
        hand_idx = deck_indices[0:7]
        lands_in_hand = np.sum(is_land[hand_idx])

        if lands_in_hand < 2 or lands_in_hand > 5:
            mull_count = 1
            hand_idx = deck_indices[7:14]
            lands_in_hand = np.sum(is_land[hand_idx])
            if lands_in_hand < 2 or lands_in_hand > 4:
                mull_count = 2

        kept_size = 7 - mull_count
        total_kept_cards += kept_size
        if mull_count > 0:
            mulligans += 1

        start_ptr = mull_count * 7

        # Turn 3 & 4 & 5 States
        t3_idx = deck_indices[start_ptr : start_ptr + kept_size + 2]
        t3_lands = np.sum(is_land[t3_idx])
        if t3_lands < 3:
            screw_t3 += 1

        t4_idx = deck_indices[start_ptr : start_ptr + kept_size + 3]
        if np.sum(is_land[t4_idx]) < 4:
            screw_t4 += 1
        if np.any(is_removal[t4_idx]):
            removal_t4 += 1

        t5_idx = deck_indices[start_ptr : start_ptr + kept_size + 4]
        if np.sum(is_land[t5_idx]) >= 6:
            flood_t5 += 1

        # Castability Logic (Bitwise Masking)
        c2, c3, c4 = False, False, False

        t2_idx = deck_indices[start_ptr : start_ptr + kept_size + 1]
        if np.sum(is_land[t2_idx]) >= 2:
            t2_colors = 0
            for idx in t2_idx:
                if is_land[idx] or is_ramp[idx]:
                    t2_colors |= mana_produced[idx]
            for idx in t2_idx:
                if not is_land[idx] and cmcs[idx] == 2:
                    if (primary_req[idx] & t2_colors) == primary_req[idx]:
                        c2 = True
                        break

        if t3_lands >= 3:
            t3_colors = 0
            has_3_drop = False
            for idx in t3_idx:
                if is_land[idx] or (is_ramp[idx] and cmcs[idx] < 3):
                    t3_colors |= mana_produced[idx]
            for idx in t3_idx:
                if not is_land[idx] and cmcs[idx] == 3:
                    has_3_drop = True
                    if (primary_req[idx] & t3_colors) == primary_req[idx]:
                        c3 = True
                        break
            if has_3_drop and not c3:
                color_screw_t3 += 1

        if np.sum(is_land[t4_idx]) >= 4:
            t4_colors = 0
            for idx in t4_idx:
                if is_land[idx] or (is_ramp[idx] and cmcs[idx] < 4):
                    t4_colors |= mana_produced[idx]
            for idx in t4_idx:
                if not is_land[idx] and cmcs[idx] == 4:
                    if (primary_req[idx] & t4_colors) == primary_req[idx]:
                        c4 = True
                        break

        if c2:
            cast_t2 += 1
        if c3:
            cast_t3 += 1
        if c4:
            cast_t4 += 1
        if c2 and c3 and c4:
            curve_out += 1

    return (
        mulligans,
        screw_t3,
        screw_t4,
        flood_t5,
        cast_t2,
        cast_t3,
        cast_t4,
        curve_out,
        removal_t4,
        color_screw_t3,
        total_kept_cards,
    )


def simulate_deck(deck_list, iterations=10000):
    arrays = _parse_deck_to_arrays(deck_list)
    if not arrays:
        return None

    is_land, is_ramp, is_removal, cmcs, mana_produced, primary_req = arrays
    results = _run_fast_monte_carlo(
        is_land, is_ramp, is_removal, cmcs, mana_produced, primary_req, iterations
    )

    return {
        "mulligans": (results[0] / iterations) * 100.0,
        "screw_t3": (results[1] / iterations) * 100.0,
        "screw_t4": (results[2] / iterations) * 100.0,
        "flood_t5": (results[3] / iterations) * 100.0,
        "cast_t2": (results[4] / iterations) * 100.0,
        "cast_t3": (results[5] / iterations) * 100.0,
        "cast_t4": (results[6] / iterations) * 100.0,
        "curve_out": (results[7] / iterations) * 100.0,
        "removal_t4": (results[8] / iterations) * 100.0,
        "color_screw_t3": (results[9] / iterations) * 100.0,
        "avg_hand_size": results[10] / iterations,
    }
