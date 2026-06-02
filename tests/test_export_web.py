"""
Tests for analysis.export_web — the parquet -> compact web context JSON exporter
(task A of the Overwolf overlay plan).

All tests build tiny parquet artifacts in a tmp dir and check the JSON the
JS front-end will consume, including that its lookups reproduce the numbers
context_advisor would compute.
"""

import json
from pathlib import Path

import duckdb
import pytest

from analysis import export_web


def _write_parquet(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    path = tmp_path / name
    json_path = tmp_path / f"{name}.json"
    json_path.write_text(json.dumps(rows))
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM read_json_auto('{json_path}')) TO '{path}' (FORMAT PARQUET)"
    )
    con.close()
    return path


@pytest.fixture
def artifacts(tmp_path):
    """A minimal set's worth of artifacts in tmp_path/artifacts."""
    art = tmp_path / "artifacts"
    art.mkdir()
    _write_parquet(art, "TST.PremierDraft.trophy_pick_stats.parquet", [
        {"card_name": "Alpha", "pick_rate_delta": 0.08, "ata_delta": -0.5, "seen_trophy": 600},
        {"card_name": "Beta", "pick_rate_delta": 0.02, "ata_delta": 0.1, "seen_trophy": 500},
        # too few trophy observations -> should be dropped
        {"card_name": "Rare", "pick_rate_delta": 0.5, "ata_delta": -2.0, "seen_trophy": 10},
    ])
    _write_parquet(art, "TST.PremierDraft.cooccurrence.trophy.parquet", [
        {"card_x": "Alpha", "card_y": "Beta", "lift": 2.5},
        {"card_x": "Beta", "card_y": "Alpha", "lift": 2.5},
        {"card_x": "Alpha", "card_y": "Gamma", "lift": 1.4},
    ])
    _write_parquet(art, "TST.PremierDraft.synergy.parquet", [
        {"card_x": "Alpha", "card_y": "Beta", "delta": 0.06},
        {"card_x": "Alpha", "card_y": "Gamma", "delta": -0.09},
    ])
    return art


def _load(out_dir):
    return json.loads((out_dir / "TST.PremierDraft.context.json").read_text())


def test_export_writes_compact_bundle(artifacts, tmp_path):
    out = tmp_path / "web"
    path = export_web.export_web("TST", "PremierDraft", artifacts_dir=artifacts, out_dir=out)
    assert path.exists()
    b = _load(out)
    assert b["set"] == "TST" and b["format"] == "PremierDraft"
    # card index covers every referenced name (incl. Gamma which only appears as a partner)
    assert set(b["cards"]) == {"Alpha", "Beta", "Gamma", "Rare"}


def test_trophy_min_seen_filter(artifacts, tmp_path):
    out = tmp_path / "web"
    export_web.export_web("TST", "PremierDraft", artifacts_dir=artifacts, out_dir=out)
    b = _load(out)
    idx = {n: i for i, n in enumerate(b["cards"])}
    assert str(idx["Alpha"]) in b["trophy"]
    # "Rare" had seen_trophy=10 (< MIN_TROPHY_SEEN) -> excluded
    assert str(idx["Rare"]) not in b["trophy"]
    # values preserved (rate_delta, ata_delta, seen_trophy)
    assert b["trophy"][str(idx["Alpha"])] == [0.08, -0.5, 600]


def test_cooc_and_synergy_lookup_roundtrip(artifacts, tmp_path):
    out = tmp_path / "web"
    export_web.export_web("TST", "PremierDraft", artifacts_dir=artifacts, out_dir=out)
    b = _load(out)
    idx = {n: i for i, n in enumerate(b["cards"])}

    # Alpha co-occurs with Beta (2.5) and Gamma (1.4), sorted by lift desc
    alpha_cooc = b["cooc"][str(idx["Alpha"])]
    assert alpha_cooc[0] == [idx["Beta"], 2.5]
    assert [idx["Gamma"], 1.4] in alpha_cooc

    # Synergy keeps sign; sorted by |delta| desc -> Gamma (-0.09) before Beta (0.06)
    alpha_syn = b["synergy"][str(idx["Alpha"])]
    assert alpha_syn[0] == [idx["Gamma"], -0.09]
    assert [idx["Beta"], 0.06] in alpha_syn


def test_cooc_falls_back_to_all_when_no_trophy(artifacts, tmp_path):
    # Remove the trophy cooc file; add an "all" file instead.
    (artifacts / "TST.PremierDraft.cooccurrence.trophy.parquet").unlink()
    _write_parquet(artifacts, "TST.PremierDraft.cooccurrence.all.parquet", [
        {"card_x": "Alpha", "card_y": "Beta", "lift": 1.9},
    ])
    out = tmp_path / "web"
    export_web.export_web("TST", "PremierDraft", artifacts_dir=artifacts, out_dir=out)
    b = _load(out)
    idx = {n: i for i, n in enumerate(b["cards"])}
    assert b["cooc"][str(idx["Alpha"])][0] == [idx["Beta"], 1.9]


def test_max_partners_truncates(artifacts, tmp_path):
    out = tmp_path / "web"
    export_web.export_web("TST", "PremierDraft", artifacts_dir=artifacts, out_dir=out, max_partners=1)
    b = _load(out)
    idx = {n: i for i, n in enumerate(b["cards"])}
    # Alpha had 2 cooc partners; capped to the strongest 1
    assert b["cooc"][str(idx["Alpha"])] == [[idx["Beta"], 2.5]]


def test_missing_artifacts_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_web.export_web("NONE", "PremierDraft", artifacts_dir=tmp_path / "artifacts", out_dir=tmp_path / "web")


def test_discover_sets(artifacts):
    pairs = export_web._discover_sets(artifacts)
    assert ("TST", "PremierDraft") in pairs
