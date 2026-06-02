# 06 — Overwolf In-Game Overlay Plan (Windows-first, v3)

> **Status:** Planning. No code yet. This is the agreed direction for a *true
> in-game overlay*, decided 2026-06-02. The existing Python/Tkinter app
> (`main.py`, `src/ui/*`) remains the cross-platform companion-window build and
> the development harness for the analysis engine — it is **not** thrown away.

## 1. Decision record

- **Goal:** an in-game overlay that, during a live Arena draft, shows 17Lands
  stats **plus our contextual layer (trophy / co-occurrence / synergy)** for the
  cards in the current pack, and a separate detail window (alt-tab) for deeper
  explanations.
- **Platform:** **Windows, via Overwolf.** Overwolf has **no macOS client**, and
  it is the sanctioned route WotC tolerates — the same one Arena Tutor,
  MTGA Assistant, and Untapped use. Windows is also ~the entire Arena audience.
- **Overlay form (v1):** an **in-game ranked panel** locked over the Arena
  window, listing every card in the pack with its stats + the `◈` context line,
  updating each pick. This is what every mature MTGA overlay actually does.
- **Per-card badges (deferred):** pinning a badge onto each card's art is **not**
  done by any shipping MTGA tool. Arena exposes *which* cards are in the pack but
  **not their on-screen coordinates**, and the draft layout shifts with
  resolution/window size. Literal per-card anchoring would require fragile
  computer vision; revisit as an experiment after v1, not before.
- **Engine unchanged:** the Python analysis engine (`analysis/`) and its parquet
  artifacts are platform-agnostic and stay as-is. The Overwolf front-end consumes
  a compact JSON export of those artifacts. This protects the most valuable work
  from the platform change (plan §12).

## 2. Why not a literal per-card overlay (the constraint)

The Arena `Player.log` gives card **identity and pick order**, never pixel
positions. Two ways to get positions, both rejected for v1:

1. **Hook the renderer** (Overwolf/injection) — Overwolf gives a transparent,
   game-locked canvas and input passthrough, but does **not** hand you each
   card's rectangle. You would still need a layout model of Arena's draft screen.
2. **Computer vision** on the captured frame — cross-platform but fragile; breaks
   on resolution, aspect ratio, hover animations, and Arena UI updates.

Conclusion: v1 = a panel listing all pack cards (robust); CV badges = a later spike.

## 3. Overwolf data sourcing (important caveat)

Overwolf's MTGA Game Events Provider (GEP) exposes `draft_pack` (pack/pick
numbers) and `draft_cards` (card ids + names), **but** GEP draft events have a
documented history of breaking for **Premier/Traditional** draft (only Quick/bot
draft was reliable). Therefore:

- **Primary source of pack contents = the `Player.log`**, parsed the same way
  `src/log_scanner.py` already does (`Draft.Notify`, `Event_PlayerDraftMakePick`,
  `BotDraft_DraftStatus` + `DraftPack`/`PickedCards`). Overwolf apps can read the
  log file directly.
- Use GEP only as a convenience/confirmation signal where it works.

This means the **log-parsing logic is the reusable core** between the Python app
and the Overwolf app — port it, don't reinvent it.

## 4. Target architecture

```
%APPDATA%\..\LocalLow\Wizards of The Coast\MTGA\Player.log
        │  (tail + parse: set/format, pack, pick, pool, taken)
        ▼
┌─────────────────────────── Overwolf app (Windows, HTML/JS/TS) ──────────────────────────┐
│  Background controller (single instance)                                                  │
│    - log tailer + draft-event parser  (ported from src/log_scanner.py)                     │
│    - draft state: current pack cards, pool, inferred colors                                │
│    - stat lookup: live 17Lands card_ratings (cached) + bundled context JSON               │
│        ├──────────────► In-game overlay window (panel locked over Arena)                   │
│        │                  every pack card: name · GIHWR · ALSA · ◈ Trophy/Lift/Syn         │
│        └──────────────► Detail/desktop window (alt-tab): full reasoning, synergies, pools  │
└───────────────────────────────────────────────────────────────────────────────────────────┘
                                   ▲
        data/artifacts/*.parquet  ─┘  (UNCHANGED Python engine; exported to a compact
        + live card_ratings cache      per-set JSON the JS app loads — see §6 task A)
```

## 5. What is reused vs. rebuilt

| Piece | Disposition |
|---|---|
| `analysis/` engine + parquet artifacts | **Reused unchanged** (Python, run per set) |
| Identity map (grpId → name) | **Reused** — note live `card_ratings` already returns `mtga_id`, so the join can lean on that |
| `src/log_scanner.py` event detection logic | **Ported** to TS (the rules, not the code) |
| `analysis/context_advisor.py` blending math | **Ported** to TS (small, well-tested logic) |
| Live 17Lands `card_ratings` fetch + cache | **Rebuilt** in the Overwolf app (with cache + descriptive UA per ToS) |
| Python/Tk UI (`src/ui/*`, `main.py`) | **Kept** as cross-platform fallback + engine dev harness |

## 6. Phased task breakdown

**A. Artifact → web bundle exporter (Python, do first — no Overwolf needed)**
- New `analysis/export_web.py`: read the committed parquet artifacts for a set and
  emit one compact `web/data/<SET>.<FORMAT>.context.json` (trophy deltas keyed by
  card, top-N co-occurrence partners per card, top-N synergy partners per card).
- Keep it small: cap partner lists, round floats. Acceptance: JSON < ~1–2 MB/set,
  loads in the browser instantly.

**B. Overwolf app skeleton**
- `manifest.json` (MTGA game id, windows: `background`, `in_game`, `desktop`),
  permissions (GameInfo, file read for the log), single-instance background.
- Empty in-game overlay window that appears when Arena is focused and is locked to
  the game window. Acceptance: overlay shows a placeholder over a running Arena.

**C. Log parser + draft state (port from `src/log_scanner.py`)**
- Tail `Player.log`; detect set/format, current pack card list, pool, taken cards;
  handle the P1P1-after-P1P2 quirk. Acceptance: replaying a captured log drives
  correct pack/pick/pool state (reuse the repo's `tests/test_log_scanner_data.py`
  fixtures as the cross-check oracle).

**D. Stats + render the panel**
- Fetch+cache live `card_ratings`; join pack grpIds → names → stats; load the
  context JSON from (A). Render the in-game panel: every pack card sorted by GIHWR
  with the `◈ Trophy ±N% · Lift N× · Syn +N%WR` line. Acceptance: live draft shows
  correct stats+context per pick, <200 ms update.

**E. Detail / alt-tab window**
- Standalone desktop window: selected-card deep dive (full synergy partners, pool
  co-occurrence, "pools like yours" once `similar_pools` is wired). Acceptance:
  clicking a card in the panel populates the detail window.

**F. Packaging / distribution**
- Overwolf app review + store listing; auto-update. FCP compliance (plan §11):
  card data + in-draft recommendations stay **free**; include the required fan
  content disclaimer; monetize only the service layer later (ad-removal/cosmetics).

## 7. Risks & open questions

- **GEP draft reliability** — assume log-based; treat GEP as optional. Re-verify
  current status during task C.
- **Overlay positioning** — v1 panel is anchored to a corner/edge of the Arena
  window, not to cards. Keep it draggable + rememberable.
- **Log path / multi-account** — confirm the Windows path and detailed-logs
  requirement in onboarding (Arena → Settings → Account → Detailed Logs).
- **Overwolf store approval** lead time and any MTGA-specific constraints.
- **Monetization** — out of scope for v1; keep the FCP rule from plan §11 intact.
- **Can't test on Mac** — all Overwolf work is authored here but verified on
  Windows; keep the Python app as the local smoke test for engine changes.

## 8. Immediate next step

Start with **task A** (the Python web-bundle exporter) — it needs no Overwolf,
runs/tests on this Mac, and unblocks the JS app's data layer. Then scaffold
task B on a Windows machine.
