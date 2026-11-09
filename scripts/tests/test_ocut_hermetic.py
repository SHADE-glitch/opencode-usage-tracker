"""test_ocut_hermetic.py — prove the autouse isolation fixture is real.

``conftest.no_real_machine`` pins every default the reader would otherwise
resolve against the developer's machine. A fixture with no test is a promise:
nothing fails if a future edit drops one ``monkeypatch.setattr``, and the leak
is exactly the one that shipped on 2026-10-13 (the plugin-events sidecar).
These cases read the patched values back and demand that each default points
inside this test's temp tree, plus a control that an ordinary fixture database
is still usable.
"""

import os
import sqlite3
from pathlib import Path

import ocut_db as db

# Captured before any test runs: HOME is still the real one at import time.
# The autouse fixture is function-scoped, so this is the machine the suite
# must not read.
REAL_HOME = Path.home()

DEFAULTS = ("HOST_CONFIG_DIR", "HOST_DB", "PLUGIN_EVENTS", "CONFIG_PATH")


def test_every_host_default_is_pinned_away_from_the_real_machine(tmp_path):
    """Positive: every import-time default is steered off the real machine."""
    for name in DEFAULTS:
        value = Path(getattr(db, name))
        assert tmp_path in value.parents, (
            f"{name} is not pinned to this test's temp tree: {value}")
        assert REAL_HOME not in value.parents, (
            f"{name} still resolves under the real home: {value}")
        assert not value.exists(), (
            f"{name} should name a path that does not exist: {value}")
    assert Path(os.environ["HOME"]) == tmp_path / "no-such-home"
    assert Path(os.environ["USERPROFILE"]) == tmp_path / "no-such-home"


def test_an_ordinary_test_database_is_left_alone(tmp_path):
    """Control: the pins do not stop a fixture building its own database."""
    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE probe (n INTEGER)")
    assert path.is_file()
