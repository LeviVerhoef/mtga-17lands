# Dev Setup

Tested on **macOS** (Python 3.14) and **Windows** (Python 3.12+).

## 1. Install dependencies

```bash
pip install -r requirements.txt        # core
pip install -r requirements-dev.txt    # + test tools
```

> **Optional:** `pip install numba` speeds up the deck Monte Carlo simulation ~5×.
> The app works correctly without it.

No Poetry required. Plain pip works on both platforms.

## 2. Run the app

**With live Arena (Windows, Arena installed and running):**

Enable **Detailed Logs (Plugin Support)** in Arena → Settings → Account first.

```bash
# Windows
run.bat

# macOS / Linux
./run.sh
```

The app auto-detects `Player.log` on all platforms:
- Windows: `%APPDATA%\..\LocalLow\Wizards of The Coast\MTGA\Player.log`
- macOS: `~/Library/Logs/Wizards of the Coast/MTGA/Player.log`
- Linux: `~/.steam/steam/steamapps/compatdata/.../Player.log`

**With a fixture log (any platform, no Arena needed):**

```bash
# One-time: build a fixture log from the existing test data
python make_fixture_log.py

# Terminal 1 — start the app pointed at the fixture
./run.sh test_logs/Player_Old_Draft.log     # macOS
run.bat test_logs\Player_Old_Draft.log      # Windows

# Terminal 2 — replay draft events at human speed (3s between picks)
python simulator.py
```

You can also drop a real `Player.log` (copied from a Windows Arena session) into
`test_logs/Player_Old_Draft.log` and the simulator will replay it — much richer
than the generated fixture.

## 3. Run tests

```bash
python -m pytest tests/ -q
```

Expected: **685 passed, 1 skipped** (as of June 2026, macOS and Windows).

## 4. Run the offline analysis pipeline

```bash
python -m analysis.export --expansion BLB --event-type PremierDraft
```

Downloads 17Lands bulk data (~270 MB compressed for BLB) and writes precomputed
parquet artifacts to `data/artifacts/`. After the one-time download + DuckDB load,
the set-based pipeline runs in ~4 min (BLB: 6M draft rows / 931k games); reruns
reuse the local DuckDB cache. The small trophy/co-occurrence/synergy artifacts are
committed to git; the larger similar-pools indexes are regenerated locally.

## 5. Export the web context bundle (for the Overwolf overlay)

```bash
python3 -m analysis.export_web --all          # every set in data/artifacts/
python3 -m analysis.export_web --expansion SOS  # a single set
```

Converts the committed parquet artifacts into a compact
`web/data/<SET>.<FORMAT>.context.json` (~0.3–0.9 MB/set) that the future
Overwolf front-end loads. The bundle includes the win-rate metrics
(GIHWR/OHWR/GDWR/GPWR/ALSA/ATA, computed from the bulk datasets, not the API)
plus the trophy / co-occurrence / synergy signals — reproducing
`analysis.context_advisor` exactly. See `docs/06-overwolf-overlay-plan.md`.

## 6. Refresh data + build the distribution manifest

```bash
python3 -m analysis.sync --sets SOS BLB        # rebuild only if S3 data changed
python3 -m analysis.sync --refresh-existing    # re-check every set with a bundle
python3 -m analysis.sync --manifest-only       # rebuild manifest.json only (no network)
python3 -m analysis.sync --sets SOS --force    # rebuild regardless
```

Before rebuilding a set, `sync` issues a cheap HEAD for its S3 dataset and only
runs the pipeline when `Last-Modified` advanced past what we last built from — so
frozen sets are never recomputed. It writes `web/data/manifest.json`
(sha256/bytes/source_last_modified/state per set), which the client diffs to
download only changed bundles. See `docs/07-data-distribution-plan.md`.

## Directory layout

```
src/           upstream overlay (log scanner, UI, 17Lands client, advisor)
identity/      Scryfall grpId→name map + set-code overrides
analysis/      offline analytics engine (trophy, co-occurrence, synergy, pools)
data/          gitignored bulk data + DuckDB; tracked artifacts/
tests/         685 unit tests; fixtures/ for real log/API snapshots
test_logs/     gitignored; put Player_Old_Draft.log here for the simulator
config.toml    all runtime configuration
requirements.txt       pip install for both platforms
requirements-dev.txt   + test tools
run.sh / run.bat       launch shortcuts
make_fixture_log.py    build test_logs/Player_Old_Draft.log from test data
simulator.py           replay a log file into the running app
```
