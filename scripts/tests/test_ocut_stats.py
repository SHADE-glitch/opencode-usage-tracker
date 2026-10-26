"""test_ocut_stats.py — the headless report CLI's output, pinned where it can be wrong.

`ocut health` is the command a person runs when they suspect capture has decayed, and
until now nothing asserted what it prints: `test_ocut_dispatcher.py` checks which
*script* a command routes to, `test_ocut_sync.py` checks the tables, and the TUI suite
checks the panels. That gap is how the unparsed line came to hold two different
findings — the reader's own holdback deadline and a host format change — inside one
number that could not be read.

The autouse `no_real_machine` fixture in `conftest.py` points `HOST_DB` and
`HOST_CONFIG_DIR` at paths that do not exist, so this module opens the machine's real
OpenCode install no more than any other file in the suite does.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ocut_db as db                                    # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_stats_under_test", str(SCRIPTS / "ocut-stats.py"))
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)


def health(db_path) -> str:
    """`ocut health` against one database, its stdout as a string."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stats.main(["--db", str(db_path), "health"])
    assert rc == 0, f"health exited {rc}"
    return out.getvalue()


def test_health_splits_the_unparsed_tally_into_known_and_new(tmp_path):
    """A total that holds a deadline and a format change tells the reader neither.

    `part_left_running` is this reader's own deadline: a `tool` part the host never
    settled is indexed as it stands, and the count is how many rows may be missing a
    status or a duration. A reason nothing here has been taught about is the host
    moving. The two must read differently, or the line is decoration.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO unparsed(reason, count) VALUES('part_left_running', 3)")
    conn.commit()
    conn.close()

    text = health(path)
    assert "(3 known, 0 not)" in text, text
    assert "NEW" not in text, "an expected reason was marked as drift"
    assert "nothing has moved" in text

    conn = db.open_db(path)
    conn.execute("INSERT INTO unparsed(reason, count)"
                 " VALUES('unknown_part_type:wat', 2)")
    conn.commit()
    conn.close()

    text = health(path)
    assert "(3 known, 2 not)" in text, text
    assert "NEW" in text
    assert "unknown_part_type:wat" in text
    assert "the format moving" in text


def vacuum(db_path) -> tuple[int, str]:
    """`ocut vacuum` against one database: its exit code and its stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stats.main(["--db", str(db_path), "vacuum"])
    return rc, out.getvalue()


def test_vacuum_compacts_the_file_after_snapshotting_it(tmp_path):
    """The one command that rewrites the whole file, checked against its own before.

    A dropped index parks its pages on the freelist instead of releasing them, so
    compaction is the only thing that actually gives the space back — and the reading
    that needed it was taken by hand and written down as "`ocut` has no such
    subcommand". What a reader cannot check for themselves is whether the rewrite kept
    the rows, so the command asserts it in its own output and so does this case.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    # Rows and then no rows: the deletion is what leaves pages on the freelist for a
    # compaction to reclaim, which is the state a schema change also leaves behind.
    conn.executemany(
        "INSERT INTO tool_calls(call_id, session_id, project, tool_name, category,"
        " status, ts, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
        [(f"prt_{i}", "ses_a", "proj", "bash", "builtin", "completed", i, 5)
         for i in range(4000)])
    conn.execute("DELETE FROM tool_calls")
    conn.execute("INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
                 " category, status, ts, duration_ms)"
                 " VALUES('prt_keep','ses_a','proj','bash','builtin','completed',1,5)")
    conn.commit()
    # The deleted pages are in `-wal` until something folds them back, and the main
    # file is what both this case and the command compare — the command checkpoints
    # for the same reason.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.stat().st_size
    assert before > 0
    conn.close()

    rc, text = vacuum(path)
    assert rc == 0, text
    assert "reclaimed  " in text, text
    assert "quick_check ok" in text and "every row count is what it was" in text, text
    assert path.stat().st_size < before, (before, path.stat().st_size)
    snapshots = sorted(db.backup_dir(path).glob("*pre-vacuum*.db"))
    assert len(snapshots) == 1, [p.name for p in snapshots]
    assert str(snapshots[0]) in text, text

    # The row that survived is still there, with the number the command promised.
    conn = db.open_db(path, readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    finally:
        conn.close()

    rc, text = vacuum(path)
    assert rc == 0, text
    assert "nothing — the file was already compact" in text, text


def test_vacuum_refuses_a_database_that_is_not_there(tmp_path, capsys):
    """The same refusal `ocut backup` gives, for the same reason: nothing to compact."""
    rc, _ = vacuum(tmp_path / "no-such.db")
    assert rc == 1
    assert "run: ocut sync" in capsys.readouterr().err


def test_health_still_says_zero_and_why_when_nothing_is_unparsed(tmp_path):
    """The empty case is printed too, so the line proves the check ran.

    Its absence would be indistinguishable from a report that forgot to look.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.close()
    text = health(path)
    assert "unparsed       0 — every record type was claimed" in text, text


# --- restore ---------------------------------------------------------------

def _live_db(tmp_path):
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO tool_calls(call_id, tool_name) VALUES('prt_keep','shell')")
    conn.commit()
    conn.close()
    return path


def test_restoring_the_oldest_snapshot_keeps_the_source_and_the_live_rows(tmp_path):
    """The pre-restore snapshot must not prune the file it is restoring.

    With a full backup directory, taking the undo copy pushes the oldest snapshot
    past the retention cutoff. The old order pruned it first and then ``connect``
    recreated it as an empty file, so restoring it replaced the live database with
    zero tables. The source now stays open read-only and is protected from the
    sweep, and the copy lands through a temporary file that is checked first.
    """
    live = _live_db(tmp_path)
    conn = db.open_db(live)
    try:
        snapshots = [db.snapshot(conn, reason=f"r{i}", db_path=live)
                     for i in range(db.BACKUP_KEEP)]
    finally:
        conn.close()
    # Pin the order independently of how fast the snapshots were taken, and pick
    # the oldest: that is the one the next prune would delete.
    for i, path in enumerate(snapshots):
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    victim = min(snapshots, key=lambda p: p.stat().st_mtime)
    assert victim.stat().st_size > 0

    db.restore(victim, db_path=live)

    assert victim.exists(), "the snapshot being restored was pruned"
    assert victim.stat().st_size > 0, "the snapshot being restored became an empty file"
    check = sqlite3.connect(live)
    try:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert check.execute(
            "SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    finally:
        check.close()
    undo = list(db.backup_dir(live).glob("*pre-restore*.db"))
    assert undo, "the restore did not snapshot the database it replaced"


def test_restore_refuses_a_damaged_backup_without_touching_the_live_file(tmp_path):
    live = _live_db(tmp_path)
    before = live.read_bytes()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database")
    with pytest.raises((sqlite3.Error, ValueError)):
        db.restore(bad, db_path=live)
    assert live.read_bytes() == before


def test_restore_refuses_an_empty_file_without_touching_the_live_file(tmp_path):
    """A zero-byte file is valid SQLite, so quick_check alone cannot reject it."""
    live = _live_db(tmp_path)
    before = live.read_bytes()
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="not an ocut database"):
        db.restore(empty, db_path=live)
    assert live.read_bytes() == before


# --- export / show scoping / health labels ---------------------------------

def test_export_carries_every_table_and_view(tmp_path, capsys):
    """The export is the incident copy: raw calls, cursors and unparsed included."""
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO tool_calls(call_id, tool_name) VALUES('prt_x','shell')")
    conn.execute("INSERT INTO sync_state(name, cursor) VALUES('part', 7)")
    conn.commit()
    expected = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        " AND name NOT LIKE 'sqlite_%'")}
    conn.close()

    assert stats.main(["--db", str(path), "export"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert expected <= set(out), sorted(expected - set(out))
    assert out["tool_calls"][0]["tool_name"] == "shell"
    assert out["sync_state"][0]["cursor"] == 7
    assert "meta" in out and "unparsed" in out and "v_tools" in out


def test_show_mcp_refuses_to_merge_two_servers_and_honours_server(tmp_path, capsys):
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    for i, server in enumerate(("alpha", "beta")):
        conn.execute(
            "INSERT INTO mcp_usage(call_id, server, tool, session_id, project, ts)"
            " VALUES(?,?,?,?,?,?)",
            (f"call_{i}", server, "search", f"ses_{i}", server, 1_700_000_000_000 + i))
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as out:
        stats.main(["--db", str(path), "show", "mcp", "search"])
    assert out.value.code == 2
    err = capsys.readouterr().err
    assert "2 servers" in err and "--server alpha" in err and "--server beta" in err

    assert stats.main(["--db", str(path), "show", "mcp", "search",
                       "--server", "alpha"]) == 0
    text = capsys.readouterr().out
    assert "mcp alpha · search" in text
    assert "alpha" in text and "beta" not in text


def test_format_write_refreshes_the_generated_block_in_place(tmp_path, monkeypatch):
    """``--write`` rewrote a file that does not exist, so it could only ever fail.

    It named ``docs/maintenance/opencode-format.md``; the generated block belongs
    in ``opencode-interface.md``, the document a reader is sent to for "which host
    names does this build read". The target is a module constant so a case can
    point it at a copy: ``--write`` rewrites a tracked file, and running it in
    place would leave the repository modified for the length of the run.
    """
    doc = tmp_path / "opencode-interface.md"
    doc.write_text(f"# Interface\n\n{stats.FORMAT_BEGIN}\nstale\n{stats.FORMAT_END}"
                   "\n\ntail\n", encoding="utf-8")
    monkeypatch.setattr(stats, "FORMAT_DOC", doc)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert stats.main(["format", "--write"]) == 0
    body = doc.read_text(encoding="utf-8")
    assert "stale" not in body
    assert "**Host tables read" in body        # the registry, not the old text
    assert body.startswith("# Interface")      # the prose around it survives
    assert body.endswith("tail\n")
    assert str(doc) in out.getvalue()


def test_format_write_refuses_a_document_without_the_markers(tmp_path, monkeypatch):
    """Refusing beats appending: a generated section in hand-written prose states
    two things at once. The refusal names the file it looked in."""
    doc = tmp_path / "no-markers.md"
    doc.write_text("# prose only\n", encoding="utf-8")
    monkeypatch.setattr(stats, "FORMAT_DOC", doc)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert stats.main(["format", "--write"]) == 1
    assert str(doc) in err.getvalue()
    assert doc.read_text(encoding="utf-8") == "# prose only\n"


def test_the_interface_document_carries_the_generated_markers():
    """The shipped document must keep them, or ``--write`` can only refuse."""
    text = stats.FORMAT_DOC.read_text(encoding="utf-8")
    assert stats.FORMAT_BEGIN in text and stats.FORMAT_END in text


def test_the_checked_in_block_is_what_the_registry_renders():
    """A generated block nobody regenerates is a hand-copied list with extra steps.

    This is the check that makes ``--write`` worth having: edit the registry and
    the document goes red until the block is refreshed.
    """
    text = stats.FORMAT_DOC.read_text(encoding="utf-8")
    _, _, rest = text.partition(stats.FORMAT_BEGIN + "\n")
    block, _, _tail = rest.partition(stats.FORMAT_END)
    # `conftest.py`'s hermetic fixture repoints the host database and the sidecar
    # at temporary files, so a path is compared by position rather than by value:
    # everything else in the block is verbatim. The `~` form is a separate
    # assertion below, because an absolute path here would publish this machine's
    # user name in a checked-in document.
    pathy = re.compile(r"`(?:~|/)[^`]*`")
    got = [pathy.sub("`<path>`", ln)
           for ln in block.splitlines()]
    want = [pathy.sub("`<path>`", ln)
            for ln in stats.db.load_sync().format_doc().splitlines()]
    assert got == want
    assert "`~/." in block


def test_health_labels_the_sidecar_in_bytes_and_the_format_as_a_marker(tmp_path):
    """A byte cursor printed as a row count invented thousands of calls."""
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO sync_state(name, cursor, host_count, host_max, note)"
        " VALUES('plugin-events', 1234, 1234, 1234, '')")
    conn.execute(
        "INSERT INTO sync_state(name, cursor, host_count, host_max, note)"
        " VALUES('host-format', 2, 0, 0, 'v2')")
    conn.execute(
        "INSERT INTO plugin_usage(call_id, plugin, kind, item, ts)"
        " VALUES('cmd_a','dcp','command','compress',1700000000000)")
    conn.commit()
    conn.close()

    text = health(path)
    assert "sidecar plugin-events 1,234 of 1,234 bytes (1 calls indexed)" in text, text
    assert "format  v2 (marker 2)" in text, text
    assert "1234 live rows" not in text, text
