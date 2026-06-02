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

Expected: **594 passed, 1 skipped** (as of June 2026, macOS and Windows).

## 4. Run the offline analysis pipeline

```bash
python -m analysis.export --expansion BLB --event-type PremierDraft
```

Downloads 17Lands bulk data (~GB range) and writes precomputed parquet artifacts
to `data/artifacts/`. Takes 10–30 min first run per set; subsequent runs use the
local DuckDB cache.

## Directory layout

```
src/           upstream overlay (log scanner, UI, 17Lands client, advisor)
identity/      Scryfall grpId→name map + set-code overrides
analysis/      offline analytics engine (trophy, co-occurrence, synergy, pools)
data/          gitignored bulk data + DuckDB; tracked artifacts/
tests/         594 unit tests; fixtures/ for real log/API snapshots
test_logs/     gitignored; put Player_Old_Draft.log here for the simulator
config.toml    all runtime configuration
requirements.txt       pip install for both platforms
requirements-dev.txt   + test tools
run.sh / run.bat       launch shortcuts
make_fixture_log.py    build test_logs/Player_Old_Draft.log from test data
simulator.py           replay a log file into the running app
```
