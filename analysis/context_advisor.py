"""
analysis/context_advisor.py

Loads precomputed parquet artifacts and annotates live Recommendation objects
with contextual signals:

  - Trophy delta: how much stronger this card is picked in 7-win decks
  - Pool lift: co-occurrence lift of this card with cards already in your pool
  - Pool synergy: win-rate uplift of this card when combined with pool cards

All lookups degrade gracefully to no-ops when artifacts are absent — the live
overlay continues working even before the pipeline has been run for a set.

Usage (called from app_controller after evaluate_pack):
    ctx = get_advisor(expansion, event_type)
    recommendations = ctx.annotate(recommendations, pool_names)
"""

import logging
from pathlib import Path
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

_ARTIFACTS = Path(__file__).parent.parent / "data" / "artifacts"

# Module-level cache keyed by (expansion, event_type) so artifacts are loaded
# once per session rather than on every pick evaluation.
_advisor_cache: dict[tuple, "ContextAdvisor"] = {}

# Score contribution weights — keep small relative to the engine's ~0–120 range.
_TROPHY_RATE_WEIGHT = 40.0   # pick_rate_delta of 0.10 → +4 pts
_POOL_LIFT_WEIGHT = 6.0      # lift of 1.3 → +1.8 pts
_POOL_SYNERGY_WEIGHT = 80.0  # wr delta of 0.05 → +4 pts

# Minimum trophy sample size to trust the delta
_MIN_TROPHY_SEEN = 50


def get_advisor(expansion: str, event_type: str) -> "ContextAdvisor":
    """Return a cached ContextAdvisor for this set/format, creating if needed."""
    key = (expansion, event_type)
    if key not in _advisor_cache:
        _advisor_cache[key] = ContextAdvisor(expansion, event_type)
    return _advisor_cache[key]


def clear_cache() -> None:
    """Evict all cached advisors (call on set change or pipeline re-run)."""
    _advisor_cache.clear()


class ContextAdvisor:
    """
    Annotates Recommendation objects with trophy/pool context.
    Artifacts are loaded lazily on first annotate() call.
    """

    def __init__(self, expansion: str, event_type: str):
        self.expansion = expansion
        self.event_type = event_type
        # card_name -> {rate_delta, ata_delta, seen_trophy}
        self._trophy: Optional[dict[str, dict]] = None
        # (card_x, card_y) -> lift
        self._cooc: Optional[dict[tuple, float]] = None
        # (card_x, card_y) -> positive wr delta
        self._synergy: Optional[dict[tuple, float]] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def annotate(self, recommendations: list, pool_names: list[str]) -> list:
        """
        Enrich each Recommendation in-place with context fields and inject
        human-readable reason strings. Returns the same list.
        """
        self._load()
        pool_set = set(pool_names)

        enriched = []
        for rec in recommendations:
            name = rec.card_name
            updates: dict = {}
            extra_reasons: list[str] = []

            # --- Trophy delta ---
            if self._trophy and name in self._trophy:
                t = self._trophy[name]
                if t.get("seen_trophy", 0) >= _MIN_TROPHY_SEEN:
                    rate_d = t.get("rate_delta")
                    ata_d = t.get("ata_delta")
                    if rate_d is not None:
                        updates["trophy_rate_delta"] = rate_d
                    if ata_d is not None:
                        updates["trophy_ata_delta"] = ata_d

                    score_bump = 0.0
                    parts = []
                    if rate_d and abs(rate_d) >= 0.02:
                        score_bump += rate_d * _TROPHY_RATE_WEIGHT
                        sign = "+" if rate_d > 0 else ""
                        parts.append(f"{sign}{rate_d*100:.1f}% rate")
                    if ata_d and abs(ata_d) >= 0.3:
                        # Negative ata_delta = taken earlier in trophy = good
                        score_bump += -ata_d * 2.0
                        sign = "+" if ata_d < 0 else ""
                        parts.append(f"{sign}{-ata_d:.1f} picks earlier" if ata_d < 0
                                     else f"{ata_d:.1f} picks later")

                    if parts:
                        extra_reasons.append("Trophy: " + ", ".join(parts))
                    updates["contextual_score"] = rec.contextual_score + score_bump

            # --- Pool lift (co-occurrence) ---
            if self._cooc and pool_set:
                lifts = [self._cooc.get((name, p), 0.0) for p in pool_set]
                positive = [l for l in lifts if l > 1.1]
                if positive:
                    avg_lift = sum(positive) / len(positive)
                    updates["pool_lift"] = avg_lift
                    bump = (avg_lift - 1.0) * _POOL_LIFT_WEIGHT
                    updates["contextual_score"] = updates.get(
                        "contextual_score", rec.contextual_score
                    ) + bump
                    extra_reasons.append(f"Pool fit: {avg_lift:.1f}× co-occur lift")

            # --- Pool synergy (win-rate conditioned) ---
            if self._synergy and pool_set:
                deltas = [self._synergy.get((name, p), 0.0) for p in pool_set]
                total = sum(d for d in deltas if d > 0)
                if total > 0.01:
                    updates["pool_synergy_delta"] = total
                    bump = total * _POOL_SYNERGY_WEIGHT
                    updates["contextual_score"] = updates.get(
                        "contextual_score", rec.contextual_score
                    ) + bump
                    extra_reasons.append(f"+{total*100:.1f}% WR with pool")

            if updates or extra_reasons:
                # Prepend context reasons so they appear first in the reasoning list
                new_reasoning = extra_reasons + list(rec.reasoning)
                updates["reasoning"] = new_reasoning
                if "contextual_score" in updates:
                    updates["contextual_score"] = round(
                        max(0.0, updates["contextual_score"]), 1
                    )
                rec = rec.model_copy(update=updates)

            enriched.append(rec)

        return enriched

    # ------------------------------------------------------------------
    # Artifact loading (lazy, called once)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._trophy = self._load_trophy()
        self._cooc = self._load_cooc()
        self._synergy = self._load_synergy()
        loaded = sum(x is not None for x in [self._trophy, self._cooc, self._synergy])
        if loaded:
            logger.info(
                "ContextAdvisor loaded %d/%d artifact(s) for %s.%s",
                loaded, 3, self.expansion, self.event_type,
            )
        else:
            logger.debug(
                "No context artifacts found for %s.%s — context layer inactive",
                self.expansion, self.event_type,
            )

    def _load_trophy(self) -> Optional[dict[str, dict]]:
        path = (
            _ARTIFACTS / f"{self.expansion}.{self.event_type}.trophy_pick_stats.parquet"
        )
        if not path.exists():
            return None
        try:
            con = duckdb.connect()
            rows = con.execute(
                f"SELECT card_name, pick_rate_delta, ata_delta, seen_trophy "
                f"FROM read_parquet('{path}')"
            ).fetchall()
            con.close()
            return {
                r[0]: {"rate_delta": r[1], "ata_delta": r[2], "seen_trophy": r[3]}
                for r in rows
            }
        except Exception as exc:
            logger.warning("Trophy artifact load failed: %s", exc)
            return None

    def _load_cooc(self) -> Optional[dict[tuple, float]]:
        # Prefer trophy co-occurrence; fall back to all-decks
        for suffix in ("trophy", "all"):
            path = (
                _ARTIFACTS
                / f"{self.expansion}.{self.event_type}.cooccurrence.{suffix}.parquet"
            )
            if not path.exists():
                continue
            try:
                con = duckdb.connect()
                rows = con.execute(
                    f"SELECT card_x, card_y, lift FROM read_parquet('{path}')"
                ).fetchall()
                con.close()
                return {(r[0], r[1]): r[2] for r in rows}
            except Exception as exc:
                logger.warning("Cooccurrence artifact load failed (%s): %s", suffix, exc)
        return None

    def _load_synergy(self) -> Optional[dict[tuple, float]]:
        path = _ARTIFACTS / f"{self.expansion}.{self.event_type}.synergy.parquet"
        if not path.exists():
            return None
        try:
            con = duckdb.connect()
            # Only positive synergy pairs — negative synergy is punished by not getting the boost
            rows = con.execute(
                f"SELECT card_x, card_y, delta FROM read_parquet('{path}') WHERE delta > 0"
            ).fetchall()
            con.close()
            return {(r[0], r[1]): r[2] for r in rows}
        except Exception as exc:
            logger.warning("Synergy artifact load failed: %s", exc)
            return None
