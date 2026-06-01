# Dev Setup

## Requirements

- Python 3.12–3.14
- pip

```bash
pip install ttkbootstrap pydantic requests Pillow pynput scipy numpy numba duckdb httpx
```

For running tests:
```bash
pip install pytest pytest-cov responses
```

## Running the app

**On Windows (with Arena installed):**
```bash
python main.py
```
Auto-detects `Player.log`. Enable **Detailed Logs (Plugin Support)** in Arena → Settings → Account first.

**On Mac (development / UI testing):**

1. Generate the fixture log from existing test data:
```bash
python make_fixture_log.py
```

2. In one terminal, start the app pointed at the fixture:
```bash
python main.py -f test_logs/Player_Old_Draft.log
```

3. In another terminal, replay the draft events at human speed:
```bash
python simulator.py
```
The overlay will update as events stream in. Use this to develop and verify UI changes without Arena.

**Supplying your own real log (any platform):**

Copy a real `Player.log` from a Windows Arena session into `test_logs/Player_Old_Draft.log`, then use the simulator. This gives the most realistic replay.

## Running tests

```bash
python -m pytest tests/ -q
```

Expected: 594 passed, 1 skipped (as of June 2026).

## Running the offline analysis pipeline

Download 17Lands bulk data and compute analysis artifacts for a set:

```bash
python -m analysis.export --expansion BLB --event-type PremierDraft
```

Artifacts are written to `data/artifacts/`. This is large (multiple GB of source data) — takes ~10–30 min first run per set.

## Directory layout

```
src/           upstream overlay (log scanner, UI, 17Lands client, advisor)
identity/      Scryfall grpId→name map
analysis/      offline analytics engine (trophy, co-occurrence, synergy, pools)
data/          gitignored bulk data + DuckDB; tracked artifacts/
tests/         594 unit tests; fixtures/ for real log/API snapshots
test_logs/     gitignored; put Player_Old_Draft.log here for the simulator
config.toml    runtime configuration
```
