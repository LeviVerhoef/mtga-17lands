"""
Quick offline check of the contextual analysis layer.

Proves that analysis.context_advisor loads the precomputed parquet artifacts for
a set and annotates draft recommendations with trophy / co-occurrence / synergy
signals — no Arena or GUI required.

Usage:
    python3 scripts/try_context.py SOS "Practiced Scrollsmith" "Postmortem Professor"
    python3 scripts/try_context.py BLB "Camellia, the Seedmiser" "Vinereap Mentor"

First arg is the set code (must have artifacts in data/artifacts/); remaining
args are the cards already in your pool. Edit PACK below to change the cards
being ranked.
"""

import os
import sys

# Allow running as `python3 scripts/try_context.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.context_advisor import get_advisor, clear_cache
from src.advisor.schema import Recommendation


def rec(name: str, score: float = 50.0) -> Recommendation:
    # Only card_name / contextual_score / reasoning matter for the context layer;
    # the rest are required by the schema so we pass harmless placeholders.
    return Recommendation(
        card_name=name,
        base_win_rate=0.55,
        contextual_score=score,
        z_score=0.0,
        cast_probability=1.0,
        wheel_chance=0.0,
        functional_cmc=2.0,
        reasoning=[],
    )


def main() -> None:
    expansion = sys.argv[1] if len(sys.argv) > 1 else "SOS"
    pool = sys.argv[2:] or ["Practiced Scrollsmith", "Postmortem Professor"]

    # Edit these to whatever pack you want to rank.
    PACK = [rec(c) for c in pool] + [rec("Plains", 30.0)]

    clear_cache()
    adv = get_advisor(expansion, "PremierDraft")
    annotated = adv.annotate(PACK, pool)

    print(f"Set: {expansion} PremierDraft   Pool: {', '.join(pool)}\n")
    for r in sorted(annotated, key=lambda x: x.contextual_score, reverse=True):
        reasons = "  ".join(f"[{s}]" for s in r.reasoning) or "(no context)"
        print(f"  {r.card_name:26s} score={r.contextual_score:6.1f}  {reasons}")


if __name__ == "__main__":
    main()
