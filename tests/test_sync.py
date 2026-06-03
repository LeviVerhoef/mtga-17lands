"""
Tests for analysis.sync — the conditional refresh driver + manifest builder.

Covers the pure logic (no network): the rebuild decision, state classification,
sha256/bytes manifest entries, and manifest round-trip / rebuild-from-disk.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis import sync

UTC = timezone.utc


def _now():
    return datetime(2026, 6, 2, tzinfo=UTC)


def _write_bundle(web: Path, expansion: str, content: str = '{"x":1}') -> Path:
    web.mkdir(parents=True, exist_ok=True)
    p = web / f"{expansion}.PremierDraft.context.json"
    p.write_text(content)
    return p


# --- file_state -----------------------------------------------------------

def test_file_state_active_vs_frozen():
    now = _now()
    recent = now - timedelta(days=10)
    old = now - timedelta(days=200)
    assert sync.file_state(recent, now=now) == "active"
    assert sync.file_state(old, now=now) == "frozen"
    assert sync.file_state(None, now=now) == "unknown"


# --- needs_rebuild --------------------------------------------------------

def test_needs_rebuild_force(tmp_path):
    b = _write_bundle(tmp_path, "SOS")
    assert sync.needs_rebuild({"source_last_modified": "2026-01-01T00:00:00+00:00"},
                              _now(), b, force=True) is True


def test_needs_rebuild_missing_bundle(tmp_path):
    missing = tmp_path / "nope.json"
    assert sync.needs_rebuild({"source_last_modified": "2026-01-01T00:00:00+00:00"},
                              _now(), missing) is True


def test_needs_rebuild_no_entry(tmp_path):
    b = _write_bundle(tmp_path, "SOS")
    assert sync.needs_rebuild(None, _now(), b) is True


def test_needs_rebuild_remote_unknown_keeps_existing(tmp_path):
    b = _write_bundle(tmp_path, "SOS")
    entry = {"source_last_modified": "2026-01-01T00:00:00+00:00"}
    # HEAD failed (remote_lm None) -> don't rebuild an existing bundle
    assert sync.needs_rebuild(entry, None, b) is False


def test_needs_rebuild_remote_newer(tmp_path):
    b = _write_bundle(tmp_path, "SOS")
    entry = {"source_last_modified": "2026-01-01T00:00:00+00:00"}
    newer = datetime(2026, 2, 1, tzinfo=UTC)
    assert sync.needs_rebuild(entry, newer, b) is True


def test_needs_rebuild_remote_same_or_older(tmp_path):
    b = _write_bundle(tmp_path, "SOS")
    entry = {"source_last_modified": "2026-02-01T00:00:00+00:00"}
    same = datetime(2026, 2, 1, tzinfo=UTC)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    assert sync.needs_rebuild(entry, same, b) is False
    assert sync.needs_rebuild(entry, older, b) is False


# --- sha256 / bundle_entry ------------------------------------------------

def test_sha256_file(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("hello")
    assert sync.sha256_file(p) == hashlib.sha256(b"hello").hexdigest()


def test_bundle_entry_fields(tmp_path):
    web = tmp_path / "web"
    content = '{"set":"SOS"}'
    _write_bundle(web, "SOS", content)
    lm = datetime(2026, 5, 14, tzinfo=UTC)
    e = sync.bundle_entry("SOS", "PremierDraft", lm, base_url="https://cdn.example.com/data",
                          web_dir=web, now=_now())
    assert e["set"] == "SOS" and e["format"] == "PremierDraft"
    assert e["state"] == "active"
    assert e["source_last_modified"] == "2026-05-14T00:00:00+00:00"
    assert e["context"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert e["context"]["bytes"] == len(content)
    assert e["context"]["url"] == "https://cdn.example.com/data/SOS.PremierDraft.context.json"


# --- manifest round-trip / rebuild ---------------------------------------

def test_write_and_load_manifest(tmp_path):
    mpath = tmp_path / "manifest.json"
    entries = [{"set": "SOS", "format": "PremierDraft"},
               {"set": "BLB", "format": "PremierDraft"}]
    sync.write_manifest(entries, mpath, now=_now())
    m = sync.load_manifest(mpath)
    # sorted by (set, format)
    assert [e["set"] for e in m["sets"]] == ["BLB", "SOS"]
    idx = sync.manifest_index(m)
    assert ("SOS", "PremierDraft") in idx


def test_cleanup_set_removes_intermediates(tmp_path, monkeypatch):
    gz1 = tmp_path / "draft.gz"; gz1.write_text("x")
    gz2 = tmp_path / "game.gz"; gz2.write_text("x")
    db = tmp_path / "set.duckdb"; db.write_text("x")
    monkeypatch.setattr(sync, "_local_gz",
                        lambda kind, e, f: gz1 if kind == "draft_data" else gz2)
    monkeypatch.setattr(sync, "_db_path", lambda e, f: db)
    sync.cleanup_set("SOS", "PremierDraft")
    assert not gz1.exists() and not gz2.exists() and not db.exists()
    # idempotent: second call with files already gone doesn't raise
    sync.cleanup_set("SOS", "PremierDraft")


def test_rebuild_manifest_preserves_source_last_modified(tmp_path):
    web = tmp_path / "web"
    _write_bundle(web, "SOS")
    _write_bundle(web, "BLB")
    mpath = web / "manifest.json"
    # seed a prior manifest with a known source_last_modified for SOS
    sync.write_manifest([
        {"set": "SOS", "format": "PremierDraft",
         "source_last_modified": "2026-05-14T12:12:22+00:00"},
    ], mpath)

    sync.rebuild_manifest_from_disk(web_dir=web, manifest_path=mpath)
    idx = sync.manifest_index(sync.load_manifest(mpath))
    # SOS keeps its recorded source_last_modified (and is classified active/frozen from it)
    assert idx[("SOS", "PremierDraft")]["source_last_modified"] == "2026-05-14T12:12:22+00:00"
    # BLB had no prior record -> None / unknown, but still gets an entry with a hash
    blb = idx[("BLB", "PremierDraft")]
    assert blb["source_last_modified"] is None
    assert len(blb["context"]["sha256"]) == 64
