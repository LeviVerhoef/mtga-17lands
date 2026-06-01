# Test Fixtures

This directory holds recorded real data for deterministic tests.

## Required files (not committed — record from live systems)

- `player_log_draft_snippet.txt` — A real Arena Player.log excerpt covering at least one full draft pack event. Enable "Detailed Logs (Plugin Support)" in Arena → Settings → Account before capturing.
- `17lands_card_ratings_sample.json` — A real response from `https://www.17lands.com/card_ratings/data?expansion=<SET>&format=PremierDraft`. Dump with: `python -c "import httpx,json; print(json.dumps(httpx.get('https://www.17lands.com/card_ratings/data?expansion=BLB&format=PremierDraft').json()[:5], indent=2))"`.
- `scryfall_identity_sample.json` — Excerpt from Scryfall default_cards bulk data containing arena_id fields.

## How to populate

Run the Phase 0 checklist from the plan:
1. Start a Premier Draft in Arena with Detailed Logs enabled.
2. After the draft, copy the relevant section of Player.log here.
3. Hit the 17Lands endpoint for the same set and save the response.
4. These files feed `tests/test_identity.py` and `tests/test_log_tailer.py`.
