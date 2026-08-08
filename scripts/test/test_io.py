"""Tests for `_io` — the atomic writer every workspace artifact goes through.

The mode-preservation cases are the load-bearing ones: `raw/source-map.json` is
the single workspace file that can hold a live credential and it is `0o600`. A
naive `tmp + os.replace` widens it to umask permissions, so these assert the
finished file's mode, not the helper's arguments.
"""

import json
import os
import stat

import pytest

from skill_from_docs import _io
from skill_from_docs._schema import SOURCE_MAP_MODE, write_source_map


SOURCE_MAP = {
    "spec_url": "https://api.example.com/openapi.json",
    "fetch_url": "https://api.example.com/openapi.json?token=sentinel",
    "sha256": "0" * 64,
}


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _leftovers(d) -> list[str]:
    return [n for n in os.listdir(d) if n.endswith(".tmp")]


# --- mode handling -----------------------------------------------------------


def test_source_map_is_0600_when_written_fresh(tmp_path):
    path = tmp_path / "raw" / "source-map.json"
    write_source_map(str(path), SOURCE_MAP)
    assert _mode(path) == 0o600


def test_source_map_stays_0600_when_the_target_already_exists(tmp_path):
    """The already-exists case is where tmp+replace differs from `open(path,"w")`.

    A plain `open` of an existing file leaves its mode alone; a replace installs
    a *new* inode whose mode came from the temp file. Seed a world-readable file
    first so a helper that forgets to chmod the temp before replacing shows up
    as 0o644 here.
    """
    path = tmp_path / "raw" / "source-map.json"
    os.makedirs(path.parent)
    path.write_text("{}\n")
    path.chmod(0o644)

    write_source_map(str(path), SOURCE_MAP)

    assert _mode(path) == 0o600
    assert json.loads(path.read_text())["fetch_url"].endswith("token=sentinel")


def test_source_map_stays_0600_across_repeated_writes(tmp_path):
    path = tmp_path / "raw" / "source-map.json"
    for _ in range(3):
        write_source_map(str(path), SOURCE_MAP)
        assert _mode(path) == 0o600


def test_explicit_mode_is_applied_before_the_replace(tmp_path):
    """No window at the wrong permissions: assert via write_json directly."""
    path = tmp_path / "secret.json"
    _io.write_json(str(path), {"a": 1}, mode=SOURCE_MAP_MODE)
    assert _mode(path) == 0o600


def test_default_mode_does_not_widen_an_existing_tight_file(tmp_path):
    """mode=None must mean "whatever the file already had", not "umask"."""
    path = tmp_path / "docs.md"
    path.write_text("old\n")
    path.chmod(0o600)

    _io.write_text(str(path), "new\n")

    assert _mode(path) == 0o600
    assert path.read_text() == "new\n"


def test_default_mode_on_a_new_file_matches_a_plain_open(tmp_path):
    written = tmp_path / "via-helper.txt"
    baseline = tmp_path / "via-open.txt"
    _io.write_text(str(written), "x\n")
    with open(baseline, "w", encoding="utf-8") as f:
        f.write("x\n")
    assert _mode(written) == _mode(baseline)


# --- atomicity ---------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    path = tmp_path / "manifest.json"
    _io.write_json(str(path), {"runs": [1]})
    before = path.read_text()

    with pytest.raises(TypeError):
        _io.write_json(str(path), {"runs": {1, 2}})  # a set is not JSON-encodable

    assert path.read_text() == before
    assert _leftovers(tmp_path) == []


def test_a_failed_replace_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """The failure can also land after the content is written. The temp must not
    survive as workspace litter that `validate` would then have to explain."""
    path = tmp_path / "out.txt"

    def boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr(_io.os, "replace", boom)
    with pytest.raises(OSError):
        _io.write_text(str(path), "payload\n")

    assert not path.exists()
    assert _leftovers(tmp_path) == []


def test_the_temp_file_lives_beside_the_target(tmp_path, monkeypatch):
    """os.replace is atomic only within a filesystem, so the temp must share the
    target's directory — not $TMPDIR, which may be a different mount."""
    seen = {}
    real_mkstemp = _io.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(_io.tempfile, "mkstemp", spy)
    target = tmp_path / "nested" / "handoff.json"
    _io.write_json(str(target), {"ok": True})

    assert seen["dir"] == str(target.parent)


# --- content -----------------------------------------------------------------


def test_write_json_shape_is_indent_2_plus_trailing_newline(tmp_path):
    path = tmp_path / "x.json"
    _io.write_json(str(path), {"b": 1, "a": [2]})
    assert path.read_text() == '{\n  "b": 1,\n  "a": [\n    2\n  ]\n}\n'


def test_write_text_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "c.md"
    _io.write_text(str(path), "hi\n")
    assert path.read_text() == "hi\n"
