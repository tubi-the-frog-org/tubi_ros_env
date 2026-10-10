import json
import logging
import os

import pytest

from pi_app.snacks import (
    SnacksConfigError,
    SnacksStore,
    load_snacks,
    parse_snacks,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
VALID_PATH = os.path.join(FIXTURES_DIR, "valid_snacks.json")
INVALID_PATH = os.path.join(FIXTURES_DIR, "invalid_missing_field.json")


def _valid_data():
    with open(VALID_PATH) as f:
        return json.load(f)


# ---- parse_snacks ----

def test_parse_valid_config():
    config = parse_snacks(_valid_data())
    assert config.version == 1
    assert config.default_snack == "none"
    assert config.ids() == ["cookies", "candy", "chips", "none"]
    cookies = config.get("cookies")
    assert cookies.slot == 1
    assert cookies.angle == 65.0
    assert cookies.aliases == ["biscuit"]
    assert cookies.lines[0].text.startswith("Ah, the ancient tides")
    assert "cookies" in config
    assert "nonexistent" not in config
    assert config.get("nonexistent") is None


def test_missing_label_raises():
    data = _valid_data()
    del data["snacks"][0]["label"]
    with pytest.raises(SnacksConfigError, match="label"):
        parse_snacks(data)


def test_duplicate_snack_id_raises():
    data = _valid_data()
    dup = dict(data["snacks"][0])
    data["snacks"].append(dup)
    with pytest.raises(SnacksConfigError, match="duplicate"):
        parse_snacks(data)


def test_unknown_default_snack_raises():
    data = _valid_data()
    data["default_snack"] = "ghost_snack"
    with pytest.raises(SnacksConfigError, match="default_snack"):
        parse_snacks(data)


def test_empty_lines_raises():
    data = _valid_data()
    data["snacks"][0]["lines"] = []
    with pytest.raises(SnacksConfigError, match="lines"):
        parse_snacks(data)


def test_non_dict_root_raises():
    with pytest.raises(SnacksConfigError, match="root must be an object"):
        parse_snacks(["not", "a", "dict"])


def test_bad_angle_type_raises():
    data = _valid_data()
    data["snacks"][0]["angle"] = "sixty-five"
    with pytest.raises(SnacksConfigError, match="angle"):
        parse_snacks(data)


# ---- load_snacks ----

def test_load_snacks_from_file():
    config = load_snacks(VALID_PATH)
    assert config.default_snack == "none"


def test_load_snacks_missing_file_raises():
    with pytest.raises(SnacksConfigError, match="could not read"):
        load_snacks(os.path.join(FIXTURES_DIR, "does_not_exist.json"))


def test_load_snacks_bad_json_raises(tmp_path):
    bad_file = tmp_path / "broken.json"
    bad_file.write_text("{not valid json")
    with pytest.raises(SnacksConfigError, match="not valid JSON"):
        load_snacks(str(bad_file))


def test_load_snacks_invalid_schema_raises():
    with pytest.raises(SnacksConfigError, match="failed validation"):
        load_snacks(INVALID_PATH)


# ---- SnacksStore ----

@pytest.fixture
def live_config_path(tmp_path):
    path = tmp_path / "snacks.json"
    path.write_text(json.dumps(_valid_data()))
    return path


def test_store_initial_load(live_config_path):
    store = SnacksStore(str(live_config_path))
    assert store.current.default_snack == "none"
    assert "cookies" in store.current


def test_store_construction_raises_on_invalid_initial_file(tmp_path):
    bad_path = tmp_path / "snacks.json"
    bad_path.write_text("{not valid json")
    with pytest.raises(SnacksConfigError):
        SnacksStore(str(bad_path))


def test_store_maybe_reload_noop_when_unchanged(live_config_path):
    store = SnacksStore(str(live_config_path))
    assert store.maybe_reload() is False


def test_store_maybe_reload_picks_up_valid_change(live_config_path):
    logger, records = _recording_logger()
    store = SnacksStore(str(live_config_path), logger=logger)
    data = _valid_data()
    del data["snacks"][2]  # drop chips
    _touch_with_new_mtime(live_config_path, json.dumps(data))

    reloaded = store.maybe_reload()

    assert reloaded is True
    assert "chips" not in store.current
    assert any("removed=['chips']" in r.getMessage() for r in records)


def test_store_maybe_reload_keeps_old_config_on_invalid_change(live_config_path):
    logger, records = _recording_logger()
    store = SnacksStore(str(live_config_path), logger=logger)
    original_ids = store.current.ids()
    _touch_with_new_mtime(live_config_path, "{not valid json")

    reloaded = store.maybe_reload()

    assert reloaded is False
    assert store.current.ids() == original_ids
    assert any("reload failed" in r.getMessage() for r in records)


def test_store_force_reload_reports_status(live_config_path):
    store = SnacksStore(str(live_config_path))
    ok, msg = store.force_reload()
    assert ok is True
    assert msg == "reloaded"

    _touch_with_new_mtime(live_config_path, "{not valid json")
    ok, msg = store.force_reload()
    assert ok is False
    assert "kept previous config" in msg


def _recording_logger():
    """A logger with a handler that just appends LogRecords to a list.

    Used instead of pytest's caplog fixture, which doesn't reliably
    capture records in this environment's ament/colcon pytest plugin
    stack.
    """
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.Logger(name=f"test.{id(records)}", level=logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_ListHandler())
    return logger, records


def _touch_with_new_mtime(path, content):
    """Write new content and nudge mtime forward so SnacksStore notices it
    even on filesystems with coarse mtime resolution."""
    path.write_text(content)
    new_mtime = os.stat(path).st_mtime + 1
    os.utime(path, (new_mtime, new_mtime))
