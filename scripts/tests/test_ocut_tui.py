"""Tests for the TUI's own contract: the eight tabs, their windows, and the
numbers they print.

Everything here is pinned against a fixture built through the real indexer
(`ocut-sync.py`), not against a hand-inserted database, so the chain
host rows -> cursor -> table -> panel is one test. The row counts and token sums
are re-read from the same database with plain SQL, so a panel that silently
switched to a different query — or forgot the window — fails here instead of
looking plausible.
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db                                    # noqa: E402
from textual.css.scalar import Unit                    # noqa: E402
from textual.widgets import DataTable                   # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_tui_under_test", str(Path(__file__).resolve().parents[1] / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

# Reuse the host-database builders from the sync suite. Importing a sibling test
# module is unusual, and deliberate: the fixture shapes (part JSON, session
# columns, the sentinel vocabulary) must be ONE definition, or the two suites
# drift and the TUI is tested against a host that no longer exists.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_sync as ts_sync                        # noqa: E402

TRACKER_TABS = ["tab-dashboard", "tab-tools", "tab-skills", "tab-agents",
                "tab-plugins", "tab-mcp", "tab-tokens", "tab-usage"]

DAY = 86_400_000


def now_ms():
    return int(datetime.now().timestamp() * 1000)


def build_host(path: Path):
    """One session with calls today and calls 40 days ago, and known tokens."""
    now = now_ms()
    old = now - 40 * DAY
    conn = ts_sync.make_host(path)
    ts_sync.add_session(conn, "ses_today", tokens=(0, 0, 0, 0, 0))
    ts_sync.add_session(conn, "ses_old", tokens=(0, 0, 0, 0, 0),
                        started=old, ended=old + 60_000)
    ts_sync.add_message(conn, "msg_today", "ses_today")
    ts_sync.add_message(conn, "msg_old", "ses_old")
    # today: 3 bash, 1 skill, 1 task, 2 mcp, 2 steps. One bash is an `error`:
    # without a non-completed row, calls == completed == errored+1 for every tool and
    # a panel that printed the wrong one of those columns would still match the
    # query. Keep this row when editing the fixture.
    for i in range(3):
        ts_sync.add_tool(conn, f"prt_bash_{i}", "msg_today", "ses_today",
                         tool="bash", ts=now - i * 60_000,
                         status="error" if i == 2 else "completed")
    ts_sync.add_tool(conn, "prt_skill", "msg_today", "ses_today", tool="skill",
                     state={"input": {"name": "triage"}}, ts=now - 5_000)
    ts_sync.add_tool(conn, "prt_task", "msg_today", "ses_today", tool="task",
                     state={"input": {"subagent_type": "explore"},
                            "metadata": {"sessionId": "ses_child"}}, ts=now - 4_000)
    for i, name in enumerate(("context7_query-docs", "context7_resolve-library-id")):
        ts_sync.add_tool(conn, f"prt_mcp_{i}", "msg_today", "ses_today", tool=name,
                         state={"input": {}}, ts=now - 3_000 + i)
    ts_sync.add_step(conn, "prt_step_a", "msg_today", "ses_today", ts=now - 2_000,
                     tokens={"total": 1000, "input": 400, "output": 100,
                             "reasoning": 50, "cache": {"read": 450, "write": 0}})
    ts_sync.add_step(conn, "prt_step_b", "msg_today", "ses_today", ts=now - 1_000,
                     tokens={"total": 2000, "input": 800, "output": 200,
                             "reasoning": 0, "cache": {"read": 1000, "write": 0}})
    # 40 days ago: 5 read calls and one step, so "Today" and "All time" differ
    for i in range(5):
        ts_sync.add_tool(conn, f"prt_read_{i}", "msg_old", "ses_old", tool="read",
                         ts=old + i * 1000)
    ts_sync.add_step(conn, "prt_step_old", "msg_old", "ses_old", ts=old + 60_000,
                     tokens={"total": 500, "input": 200, "output": 50,
                             "reasoning": 0, "cache": {"read": 250, "write": 0}})
    # update the rollups so the two granularities still agree
    conn.execute("UPDATE session SET tokens_input=1200, tokens_output=300,"
                 " tokens_reasoning=50, tokens_cache_read=1450 WHERE id='ses_today'")
    conn.execute("UPDATE session SET tokens_input=200, tokens_output=50,"
                 " tokens_reasoning=0, tokens_cache_read=250 WHERE id='ses_old'")
    conn.commit()
    conn.close()


def build_config(root: Path):
    cfg = root / "opencode.json"
    root.mkdir(parents=True, exist_ok=True)
    skill = root / "skills" / "personal" / "a-very-long-skill-name-for-clipping"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: a-very-long-skill-name-for-clipping\n---\nbody\n")
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"context7": {"url": "https://x"}},
        "plugin": ["@acme/demo-plugin@1.2.3", "./plugins/local-one.js"],
        "provider": {}, "command": {}}))
    return root


@pytest.fixture
def tracker(tmp_path):
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    return out


@pytest.fixture
def clock(tracker):
    """`now` pinned after the fixture database is written (seconds, as the app wants).

    Re-deriving the window from the current time is right for a long-running session
    and wrong for a test that compares two derivings: they must see the same `now`,
    or the assertion is measuring the wall clock instead of the wiring.

    The app stores the callable (`self._clock = clock or time.time`), so this yields
    a function, not a number — and the number is read once, here. `lambda: now_ms()`
    would be a pinned clock that re-reads the wall clock on every call, which is how
    this fixture first produced a one-millisecond "regression" in a bounds comparison.
    """
    pinned = now_ms() / 1000
    return lambda: pinned


def drive(app_setup, body, size=(150, 55)):
    """Run `body(pilot, app)` inside a live app; surfaces any exception as a failure."""
    async def _it():
        app = app_setup()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.7)
            await body(pilot, app)
    asyncio.run(_it())


def headers(table):
    return [str(table.columns[k].label) for k in table.columns]


async def activate(app, pilot, pane: str) -> None:
    """Switch to a tab the way a person does — from the tab strip.

    ``#tab-tools`` is the TabPane, which is hidden until it is active, so clicking
    it hits nothing and the app quietly stays where it was. The strip's own id is
    ``--content-tab-<pane>``. The assert is the reason this is a helper rather than
    a line: only the active tab is re-queried on a window change, so a click that
    never landed would make every later reading a picture of a tab nobody opened.
    """
    from textual.widgets import TabbedContent
    await pilot.click(f"#--content-tab-{pane}")
    await pilot.pause(0.6)
    active = app.query_one(TabbedContent).active
    assert active == pane, f"the click did not switch tabs; still on {active}"


def cell(table, i, j):
    return str(table.get_cell_at((i, j)))


def first_column(table):
    return [cell(table, i, 0) for i in range(table.row_count)]


def value_labels(app, selector: str) -> list[str]:
    """The label half of every panel line under `selector`.

    A panel *title* ("Cache", "Cache breakdown") and a prose line ("usage unavailable:
    …") are `Static` widgets inside `.usage-panel` too, and neither carries the
    `[/dim]` marker that `_panel_line()` always emits. Harvesting every Static made
    furniture into metrics — the first version of this check "found" two cache numbers
    called Cache and Cache breakdown.
    """
    labels = []
    for w in app.query(f"{selector} Static"):
        text = str(getattr(w, "content", ""))
        if "[/dim]" not in text:
            continue
        labels.append(" ".join(text.split()).split("[/dim]")[0]
                      .replace("[dim]", "").strip())
    return labels


# --- the tab set -----------------------------------------------------------

def test_a_zero_call_row_never_reads_in_use(tmp_path):
    """The state cell agrees with the count beside it, on the tabs that still
    carry a state.

    `ever_used` is un-windowed, so a skill called only outside the tab's range
    used to print "in use" next to `calls = 0` — two cells on one row
    contradicting each other. It is now reachable only behind `a`, because the
    default list is used-in-this-window; the label still follows the window.
    """
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    conn = sqlite3.connect(out)
    conn.execute("INSERT INTO inventory(kind, name, owner_plugin, source)"
                 " VALUES('skill','old-only','','fixture')")
    conn.execute(
        "INSERT INTO skill_usage(call_id, skill, session_id, project, ts, status)"
        " VALUES('o1','old-only','ses_old','/proj',?,'completed')",
        (now_ms() - 40 * DAY,))
    conn.commit()
    conn.close()

    async def body(pilot, app):
        await activate(app, pilot, "tab-skills")
        table = app.query_one("#t-skills", DataTable)
        heads = headers(table)
        assert tui._STATE in heads, heads
        state_at = heads.index(tui._STATE)
        calls_at = heads.index("calls")
        assert "old-only" not in first_column(table), \
            "a skill with no call in the window must stay behind `a`"
        await pilot.press("a")
        await pilot.pause(0.4)
        rows = {cell(table, i, 0): (cell(table, i, calls_at),
                                    cell(table, i, state_at))
                for i in range(table.row_count)}
        assert rows["old-only"] == ("0", db.ENTITY_STATE_INSTALLED), rows["old-only"]
        # The contradiction this pins: nothing with a 0 in `calls` may claim to
        # be in use, and nothing in use may show 0.
        for name, (count, state) in rows.items():
            if state == db.ENTITY_STATE_USED:
                assert count != "0", (name, count)
            if count == "0":
                assert state != db.ENTITY_STATE_USED, (name, state)

    drive(lambda: tui.TrackerApp(out, clock=lambda: now_ms() / 1000), body)


def test_a_newline_in_a_stored_name_cannot_double_a_row(tmp_path):
    """A host-derived name can carry a newline (a malformed model call did:
    `REPO<…\n<… parameter name=`). Textual renders that as a second line, so one
    cell doubles its row's height and shifts every column after it. The stored
    value is left alone; the cell that is painted is flattened.
    """
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    conn = sqlite3.connect(out)
    conn.execute(
        "INSERT INTO tool_calls(call_id, session_id, project, tool_name, category,"
        " status, ts) VALUES('x1','ses_today','/proj','REPO<\n<parameter name=',"
        "'builtin','completed',?)", (now_ms(),))
    conn.commit()
    conn.close()

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        names = [cell(table, i, 0) for i in range(table.row_count)]
        assert names, "the fixture has tool calls"
        assert not any("\n" in n for n in names), names
        assert any(n.startswith("REPO< ") and "parameter name=" in n
                   for n in names), names
        # One line per row: a doubled cell would make the rendered table taller
        # than its rows, which is the defect this flattens.
        assert table.virtual_size.height <= table.row_count + 2, (
            table.virtual_size.height, table.row_count)

    drive(lambda: tui.TrackerApp(out, clock=lambda: now_ms() / 1000), body)


def test_the_tracker_shows_cbuts_eight_tabs_in_order(tracker):
    async def body(pilot, app):
        from textual.widgets import Tab
        panes = [x.id for x in app.query("TabPane")]
        assert panes == TRACKER_TABS, panes
        # TabPane keeps no title of its own; the tab strip is what a person reads,
        # and its ids name the pane it switches to.
        tabs = [(x.id, str(x.label)) for x in app.query(Tab)]
        assert [label for _id, label in tabs] == [
            "Dashboard", "Tools", "Skills", "Agents", "Plugins", "MCP", "Tokens",
            "Usage"], tabs
        assert [_id.replace("--content-tab-", "") for _id, _l in tabs] == TRACKER_TABS
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_the_tui_disables_textual_animations(tracker):
    """The tab-bar underline animation is off, and only ``"none"`` turns it off.

    Textual's ``Tabs._highlight_active`` animates the underline at level
    ``"basic"``, which the app-wide ``"basic"`` setting does **not** suppress — a
    tab switch pays the slide unless ``animation_level`` is exactly ``"none"``.
    This pins the level so a later edit cannot quietly re-enable it; it is the
    one place the value is asserted, so removing the assignment turns it red.
    """
    app = tui.TrackerApp(str(tracker))
    assert app.animation_level == "none", (
        "the TUI must set animation_level='none'; any other level leaves Textual's "
        f"tab-underline animation running (got {app.animation_level!r})")


def test_every_tab_has_its_own_window_and_no_default_is_all_time(tracker):
    """The parity claim: one window per tab, not one window for the app.

    Every pane owns a range, Plugins included — its *list* is all-time (the
    inventory carries no timestamps) but ``uses``/``last used`` are windowed like
    every other count, so it has a range to carry. The exact dict is asserted so a
    tab cannot quietly fall back to the app default.
    """
    async def body(pilot, a):
        assert a.tab_range == {
            "tab-dashboard": "1d", "tab-usage": "1d",
            "tab-tools": "7d", "tab-skills": "7d", "tab-agents": "7d",
            "tab-plugins": "7d", "tab-mcp": "7d", "tab-tokens": "7d",
        }, a.tab_range
        for pane, value in a.tab_range.items():
            if not isinstance(value, db.CustomWindow):
                assert value in db.USAGE_RANGES, (pane, value)
        a.tab_range["tab-tools"] = "all"
        assert a.tab_range["tab-skills"] != "all"
        assert a.tab_range["tab-agents"] != "all"
        assert a.tab_range["tab-dashboard"] != "all"
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- numbers ----------------------------------------------------------------

def test_the_tools_tab_prints_exactly_its_own_window_of_the_tools_query(
        tracker, clock):
    """The tab's list is `q_tools` at the app's own bounds — names, order, counts.

    The bounds are read back from `_bounds_for` with the app's clock pinned (see the
    `clock` fixture), so neither side of the comparison re-reads the wall clock. What
    is pinned is the wiring: this pane, this query, these bounds, these columns — and
    `window_bounds` itself is measured directly in `test_ocut_windows.py`.

    ``7d → all → 7d``: the fixture gives `read` 0 calls in a week and 5 all-time, so
    the middle step must differ and the last must come back. A tab that latched onto
    the widest window it was ever shown would fail the third step.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row          # as the app opens it

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        assert headers(table) == list(tui.TOOLS_COLUMNS), headers(table)
        # The default list is the window's *used* rows; `a` adds the installed
        # but idle ones. The exact-query comparison runs on the full union.
        await pilot.press("a")
        await pilot.pause(0.4)
        default = app.tab_range["tab-tools"]
        seen = {}
        cols = [list(tui.TOOLS_COLUMNS).index(k) for k in ("calls", "ok", "fail")]
        for rng in (default, "all", default):
            app.tab_range["tab-tools"] = rng
            app._refresh_active_tab()
            await pilot.pause(0.6)
            start, end = app._bounds_for("tab-tools")
            assert (start, end) == db.window_bounds(rng, app._now_ms()), (rng,)
            rows = list(db.q_tools(conn, start_ts=start, end_ts=end))
            assert rows, f"nothing in the {rng} window"
            # The fixture must keep one non-completed call, or `calls` and `ok`
            # hold the same number for every row and a swapped column is invisible.
            assert any(r["calls"] != r["completed"] for r in rows), \
                "fixture has no failed call: the ok column cannot be told apart"
            assert first_column(table) == [r["tool_name"] for r in rows], (rng, rows)
            for i, r in enumerate(rows):
                assert [cell(table, i, j) for j in cols] == \
                    [f"{r['calls']:,}", f"{r['completed']:,}", f"{r['errored']:,}"], \
                    (rng, r)
            seen[rng] = [cell(table, i, cols[0]) for i in range(table.row_count)]
        assert seen[default] != seen["all"], (default, seen)
        # And the default really hides the idle rows, rather than merely ranking
        # them last: `a` is the one key that changes the row set.
        await pilot.press("a")
        await pilot.pause(0.4)
        start, end = app._bounds_for("tab-tools")
        used = [r["tool_name"] for r in db.q_tools(conn, start_ts=start, end_ts=end)
                if r["calls"]]
        assert first_column(table) == used, (first_column(table), used)
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_the_tools_tab_counts_a_call_still_in_flight(tracker, clock):
    """`run` is the `running` status, not a fourth spelling of failed.

    The fixture's only non-completed call is an `error` (kept on purpose, see the
    window test above), so a `running` call is added by hand here — and the
    panel must give it its own column, because "how many are in flight" is the
    question a count of errors cannot answer.
    """
    conn = sqlite3.connect(tracker)
    ts = int(clock() * 1000) - 60_000   # inside every window the fixture spans
    conn.execute(
        "INSERT INTO tool_calls(call_id, session_id, tool_name, category,"
        " status, ts) VALUES('prt_running', 'ses_today', 'bash', 'builtin',"
        " 'running', ?)", (ts,))
    conn.commit()

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        assert headers(table) == list(tui.TOOLS_COLUMNS), headers(table)
        at = list(tui.TOOLS_COLUMNS).index("run")
        # The fixture's `bash` calls list under the merged (v2) name.
        row = first_column(table).index("shell")
        assert cell(table, row, at) == "1", (
            first_column(table), [cell(table, row, j)
                                  for j in range(len(tui.TOOLS_COLUMNS))])
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()

def test_the_window_changes_what_the_dashboard_counts(tracker, clock):
    """The same pane, two windows, two answers — the fixture spans two days.

    The expected counts are SELECTed at the bounds the app itself derived, so this
    pins "the dashboard applies its own window" rather than re-deriving midnight a
    second time in the test.
    """
    conn = sqlite3.connect(tracker)

    def count(start, end):
        return conn.execute("SELECT COUNT(*) FROM tool_calls"
                            " WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)",
                            (start, start, end, end)).fetchone()[0]

    async def body(pilot, app):
        assert app.tab_range["tab-dashboard"] != "all"
        assert count(None, None) != count(*db.window_bounds("1d", app._now_ms())), \
            "the fixture must span two days for this to mean anything"
        seen = {}
        for rng in ("1d", "all", "1d"):
            app.tab_range["tab-dashboard"] = rng
            app._refresh_active_tab()
            await pilot.pause(0.6)
            start, end = app._bounds_for("tab-dashboard")
            # The pane's key goes through the one shared window function; nothing
            # here re-derives midnight, so a panel with its own private arithmetic
            # would disagree with `test_ocut_windows.py`'s direct readings.
            assert (start, end) == db.window_bounds(rng, app._now_ms()), (rng,)
            shown = int(str(app.query_one("#dash-kpi-tools").content)
                        .split()[-1].replace(",", ""))
            assert shown == count(start, end), (rng, shown, start, end)
            seen[rng] = shown
        assert seen["1d"] != seen["all"], seen
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_the_dashboard_draws_no_runtime_or_calls_per_day_panel(tracker, clock):
    """Three rows: headline counts, the leaderboards, the project ledger.

    The Runtime and Calls-per-day panels answered questions every other tab
    already answers better (the Tools tab has the failure count; the Usage page
    has the per-day history), and they cost a whole dashboard row. Removing them
    is the layout, so the absence is what this pins — a leftover id would mean a
    panel came back without its numbers.
    """
    async def body(pilot, app):
        await activate(app, pilot, "tab-dashboard")
        for gone in ("#dash-activity", "#dash-activity-title",
                     "#dash-activity-axis", "#dash-cost", "#dash-cost-axis",
                     "#dash-rt-failed", "#dash-rt-running", "#dash-rt-subagents",
                     "#dash-rt-compactions", "#dash-rt-peak"):
            assert not app.query(gone), f"{gone} should be gone"
        rows = [w.id for w in app.query(".dash-row")]
        assert rows == ["dash-row-1", "dash-row-2", "dash-row-3"], rows
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)


def test_the_project_ledger_takes_the_height_the_rows_leave(tracker, clock):
    """A content-sized dashboard ended above the status line on a tall screen.

    The ledger is the only panel that can use extra rows, so it grows into
    them; on a short screen it keeps its natural height and the box scrolls, so
    the growth can never clip a project.
    """
    from textual.widgets import Static

    async def tall(pilot, app):
        box = app.query_one("#dash-box")
        table = app.query_one("#dash-projects", DataTable)
        note = app.query_one("#dash-note", Static)
        assert box.max_scroll_y == 0, "a tall dashboard still scrolls"
        assert table.size.height > 1 + table.row_count, (
            "the ledger never took the spare height")
        assert note.region.bottom == box.region.bottom, (
            "the ledger did not reach the bottom of the box")
        # The fit is re-run when the box itself resizes: the app's resize event
        # arrives before the box is laid out at its new height, so a fit
        # measured there would use the previous window's spare.
        await pilot.resize_terminal(160, 60)
        await pilot.pause()
        await pilot.pause()
        assert box.max_scroll_y == 0
        assert note.region.bottom == box.region.bottom, (
            "growing the terminal left the ledger behind")
        await pilot.resize_terminal(120, 20)
        await pilot.pause()
        await pilot.pause()
        assert box.max_scroll_y > 0, "shrinking the terminal did not scroll"
        await pilot.resize_terminal(120, 45)
        await pilot.pause()
        await pilot.pause()
        assert box.max_scroll_y == 0
        assert note.region.bottom == box.region.bottom, (
            "the ledger did not take the height back")

    async def short(pilot, app):
        box = app.query_one("#dash-box")
        table = app.query_one("#dash-projects", DataTable)
        assert table.size.height == 1 + table.row_count, (
            "a short box grew the ledger instead of scrolling")
        assert box.max_scroll_y > 0, "the short box does not scroll"

    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), tall, size=(120, 45))
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), short, size=(120, 20))


def test_token_panels_print_the_hosts_own_step_numbers(tracker, clock):
    """The Tokens panel sums `model_calls` — the per-step granularity, not the rollup.

    Summed at the bounds the app derived, so the panel cannot pass by accident while
    reading a different table or a different window. The labels are CodeBuddy's:
    ``Input`` is the full prompt (hit + miss + write) and ``Usage Total`` re-adds the
    cache hit, so both are derived from the three OpenCode cache numbers here.
    """
    conn = sqlite3.connect(tracker)

    async def body(pilot, app):
        app.tab_range["tab-dashboard"] = "all"
        app._refresh_active_tab()
        await pilot.pause(0.6)
        start, end = app._bounds_for("tab-dashboard")
        rows = conn.execute(
            "SELECT prompt_tokens, completion_tokens, cache_read, cache_write,"
            " total_tokens FROM model_calls"
            " WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)",
            (start, start, end, end)).fetchall()
        assert rows, f"nothing in {start}..{end}"
        prompt = sum(r[0] for r in rows)          # uncached input = cache miss
        completion = sum(r[1] for r in rows)
        read = sum(r[2] for r in rows)            # cache read = cache hit
        write = sum(r[3] for r in rows)
        total = sum(r[4] for r in rows)
        inp = prompt + read + write               # full prompt = CodeBuddy's Input
        got = {w.id: str(w.content) for w in app.query("#dash-tok-input,"
                                                      " #dash-cache-hit,"
                                                      " #dash-tok-usage")}
        # Panel lines use the compact form so a 12-digit total cannot push the
        # line into a second row; the tables keep the exact comma-grouped value.
        assert tui.fmt_compact_n(inp) in got["dash-tok-input"], got
        assert tui.fmt_compact_n(read) in got["dash-cache-hit"], got
        # The API total has no panel: one token total, and it is the Usage Total.
        assert not app.query("#dash-tok-api")
        # Usage Total = Input + Output + cache hit, and it is NOT the host total
        assert tui.fmt_compact_n(inp + completion + read) in got["dash-tok-usage"], got
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_the_dashboard_shows_real_money_and_leaves_failures_to_the_tools_tab(
        tracker, clock):
    """Cost is a number on the dashboard; the failure/running split is not.

    The dashboard used to answer "how many failed, how many running" in a
    Runtime panel that summed them into one "Incomplete" figure the Tools tab
    disagreed with. The panel is gone and the Tools tab is the one place that
    answers it, from the same rows — so this checks both halves: money on the
    dashboard, the two counts (and not their sum) on Tools.
    """
    conn = sqlite3.connect(tracker)
    ts = int(clock() * 1000) - 60_000
    conn.execute(
        "INSERT INTO tool_calls(call_id, session_id, tool_name, category,"
        " status, ts) VALUES('prt_dash_run', 'ses_today', 'bash', 'builtin',"
        " 'running', ?)", (ts,))
    conn.execute(
        "INSERT INTO model_calls(call_id, model, provider, prompt_tokens,"
        " completion_tokens, total_tokens, cost, ts)"
        " VALUES('prt_dash_cost', 'model-x', 'vendor', 10, 5, 15, 0.0125, ?)",
        (ts,))
    conn.commit()

    async def body(pilot, app):
        app.tab_range["tab-dashboard"] = "all"
        app._refresh_active_tab()
        await pilot.pause(0.6)
        cost = conn.execute("SELECT SUM(cost) FROM model_calls").fetchone()[0]
        shown = str(app.query_one("#dash-sum-cost").content)
        assert f"${cost:.4f}" in shown, shown
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        heads = headers(table)
        rows = [{heads[i]: cell(table, r, i) for i in range(len(heads))}
                for r in range(table.row_count)]
        # The tab's two columns, summed over every tool, are the two counts the
        # Runtime panel used to add together.
        failed = conn.execute("SELECT COUNT(*) FROM tool_calls"
                              " WHERE status='error'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM tool_calls"
                               " WHERE status IN ('running','streaming')"
                               ).fetchone()[0]
        assert sum(int(r["fail"]) for r in rows) == failed, rows
        assert sum(int(r["run"]) for r in rows) == running, rows
        assert failed != failed + running, "the sum is not either count"
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_the_request_log_says_how_many_rows_it_is_showing(tracker, clock):
    """The request log holds `USAGE_LOG_LIMIT` rows; a longer window is cut,
    and the cut must be on the note, not in the row count.

    The note used to deny a cost column that OpenCode's per-step price makes
    real; while that sentence was being corrected, the "how many am I not
    seeing" answer was added beside it — the same honesty the wide plan applies
    to columns.
    """
    conn = sqlite3.connect(tracker)
    ts = int(clock() * 1000) - 60_000
    limit = tui.TrackerApp.USAGE_LOG_LIMIT
    for i in range(limit + 20):   # fixture steps + these must exceed the limit
        conn.execute(
            "INSERT INTO model_calls(call_id, model, provider, prompt_tokens,"
            " completion_tokens, total_tokens, ts) VALUES(?,?,?,?,?,?,?)",
            (f"prt_bulk_{i}", "model-x", "vendor", 10, 5, 15, ts - i))
    conn.commit()

    async def body(pilot, app):
        await activate(app, pilot, "tab-usage")
        note = " ".join(str(app.query_one("#usage-note").content).split())
        # The window's own step count, not the number just inserted: the fixture
        # also carries an old step (40 days back, outside the 1d default) and two
        # today, and the note must count the window's three, no more.
        start, end = app._bounds_for("tab-usage")
        total = conn.execute(
            "SELECT COUNT(*) FROM model_calls"
            " WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)",
            (start, start, end, end)).fetchone()[0]
        assert f"showing {limit:,} of {total:,}" in note, note
        table = app.query_one("#t-usage", DataTable)
        assert table.row_count == limit, table.row_count
        # The Cost column is in the header, and the note no longer denies it.
        assert "Cost" in headers(table), headers(table)
        assert "no cost" not in note, note
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()

def test_the_cache_breakdown_uses_codebuddys_three_labels(tracker):
    """The cache breakdown carries CodeBuddy's three labels, not two.

    CodeBuddy's panel is hit / miss / write; OpenCode's uncached ``prompt_tokens`` IS
    its miss, so the three labels map onto read / input / write. Pinned as labels
    rather than values: two different metrics can legitimately coincide, but the
    vocabulary is a contract. The Dashboard keeps the bordered panel; Requests
    states the same three labels on its compact summary line instead, so both
    surfaces are read here.
    """
    async def body(pilot, app):
        app.tab_range["tab-dashboard"] = "all"
        app.refresh_data()
        await pilot.pause(0.5)
        labels = value_labels(app, "#tab-dashboard .usage-panel")
        assert len(labels) >= 9, f"the dashboard has no panel lines: {labels}"
        assert "Usage Total" in labels and "Cache miss" in labels, labels
        cache = value_labels(app, "#panel-dash-cache")
        assert {"Cache hit", "Cache miss", "Cache write"} <= set(cache), cache

        await activate(app, pilot, "tab-usage")
        app.tab_range["tab-usage"] = "all"
        app.refresh_data()
        await pilot.pause(0.5)
        note = " ".join(str(app.query_one("#usage-note").content).split())
        for label in ("Cache hit", "Cache miss", "Cache write"):
            assert label in note, note
        # The token/money line no longer repeats the cache counts, so it stays on
        # one row at a 160-column terminal.
        summary = " ".join(str(app.query_one("#usage-summary").content).split())
        assert "Usage Total" in summary and "Cache" not in summary, summary
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- the absent metrics ----------------------------------------------------

def test_the_plugins_tab_shows_a_uses_column(tracker):
    """`uses` is real now — the hook writes it, so 0 means "nothing recorded".

    Pinned as the exact header list, so changing the column set is a decision with a
    red test in front of it rather than a quiet edit. The fixture runs `sync` with no
    hook sidecar, so both plugins show 0 uses — the honest reading of "the hook
    recorded nothing", never "not used". `state` is last, as in CodeBuddy's TUI.
    """
    async def body(pilot, app):
        await activate(app, pilot, "tab-plugins")
        table = app.query_one("#t-plugins", DataTable)
        assert headers(table) == ["plugin", "uses", "tools", "skills", "agents",
                                  "cmds", "last used", "state"], headers(table)
        assert table.row_count == 2, first_column(table)
        rows = {cell(table, i, 0): tuple(cell(table, i, j) for j in range(1, 8))
                for i in range(table.row_count)}
        assert rows["demo-plugin"] == ("0", "0", "0", "0", "0", "-",
                                       "installed"), rows
        assert rows["local-one"] == ("0", "0", "0", "0", "0", "-",
                                     "installed"), rows
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_the_plugins_list_is_all_time_but_its_uses_follow_the_window(tracker):
    """The list never shrinks; the counts do — the shape every other tab uses.

    Two hook calls for one plugin, one ten days old and one a minute old: at the
    default 7d window only the recent one counts, and at all-time both do. The
    plugin with no call in range stays on the list with 0 rather than vanishing,
    because the inventory is not windowed. This is the assertion that would have
    caught the Plugins tab ignoring its bounds entirely.
    """
    conn = sqlite3.connect(tracker)
    now = now_ms()
    conn.executemany(
        "INSERT INTO plugin_usage (call_id, plugin, kind, item, session_id, ts)"
        " VALUES (?, 'demo-plugin', 'tool', 'demo-tool', ?, ?)",
        [("prt_plug_old", "ses_old", now - 10 * DAY),
         ("prt_plug_recent", "ses_today", now - 60_000)])
    conn.commit()
    conn.close()

    async def body(pilot, app):
        await activate(app, pilot, "tab-plugins")
        table = app.query_one("#t-plugins", DataTable)

        def uses():
            return {cell(table, i, 0): cell(table, i, 1)
                    for i in range(table.row_count)}

        assert app.tab_range["tab-plugins"] == "7d", app.tab_range
        assert uses()["demo-plugin"] == "1", uses()
        assert uses()["local-one"] == "0", uses()
        app.tab_range["tab-plugins"] = "all"
        app._refresh_active_tab()
        await pilot.pause(0.6)
        assert uses()["demo-plugin"] == "2", uses()
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- the state column ------------------------------------------------------

ENTITY_TABS = [("tab-tools", "t-tools", tui.TOOLS_COLUMNS),
               ("tab-skills", "t-skills", tui.SKILLS_COLUMNS),
               ("tab-agents", "t-agents", tui.AGENTS_COLUMNS),
               ("tab-plugins", "t-plugins", tui.PLUGINS_COLUMNS),
               ("tab-mcp", "t-mcp", tui.MCP_COLUMNS)]

STATES = {db.ENTITY_STATE_USED, db.ENTITY_STATE_INSTALLED, db.ENTITY_STATE_REMOVED,
          db.ENTITY_STATE_UNREPORTED}


def test_only_censused_tabs_print_a_state_column(tracker):
    """`state` is a claim, so only the kinds that can be censused may show it.

    Tools and Agents used to end on `state` and printed `not reported` on every
    row that mattered: OpenCode's builtin tool ids live in its compiled registry,
    which a passive reader cannot enumerate. The column is gone from both tabs and
    replaced by something the host does know (`category`, `children`). For the
    three censused kinds it stays last, so a narrow terminal drops the tail — not
    the answer to "is this still installed".
    """
    for tab in tui.ENTITY_TABS:
        has_state = db.ENTITY_STATE_COLUMN in tab.columns
        assert has_state == db.ENTITY_CENSUS.get(tab.kind, False), \
            (tab.kind, tab.columns)
        if has_state:
            assert tab.columns[-1] == db.ENTITY_STATE_COLUMN, tab.columns
    assert tui.TOOLS_COLUMNS[-1] == "category", tui.TOOLS_COLUMNS
    assert tui.AGENTS_COLUMNS[-1] == "last used", tui.AGENTS_COLUMNS
    assert tui.MCP_COLUMNS[0] == "server", tui.MCP_COLUMNS


def test_each_censused_tab_renders_a_state_label_from_the_shared_helper(tracker):
    """Header from the tuple, cell from `db.entity_state` — and one of the three
    labels, never anything else and never a blank.

    The empty-state row spans the label set with "-" padding, so it is skipped by
    name rather than by row index: its state cell is a filler, not a claim.
    Skills opens its full union with `a` so the installed-but-idle rows are
    checked too, not just the ones with a call in the window.
    """
    state_tabs = [entry for entry in ENTITY_TABS
                  if db.ENTITY_STATE_COLUMN in entry[2]]

    async def body(pilot, app):
        for pane, table_id, columns in state_tabs:
            await activate(app, pilot, pane)
            await pilot.press("a")      # a no-op on always-all tabs
            await pilot.pause(0.4)
            table = app.query_one(f"#{table_id}", DataTable)
            assert headers(table) == list(columns), (pane, headers(table))
            at = list(columns).index(db.ENTITY_STATE_COLUMN)
            for i in range(table.row_count):
                value = str(table.get_cell_at((i, at)))
                if value == "-":
                    continue          # empty-state filler, not an entity
                assert value in STATES, (pane, i, value)
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_a_kind_we_cannot_census_never_puts_a_state_on_a_tool_row(tracker):
    """The bug the fourth state exists for, now answered by omission.

    A builtin the host has no census of used to read `removed` on the Tools tab
    (39 of 40 rows on this host), because "not in inventory" was treated as "you
    deleted this". The Tools and Agents tables no longer carry a `state` column
    at all — the shared helper still knows the `not reported` label for the
    headless reports, but the TUI cannot make a claim the database cannot support.
    A *censused* skill that no longer appears in the inventory still reads
    `removed`, dimmed, so the label survives where it means something.
    """
    conn = sqlite3.connect(tracker)
    conn.execute(
        "INSERT INTO skill_usage(call_id, skill, session_id, project, ts, status)"
        " VALUES('prt_gone','gone-skill','ses_today','/proj',?,'completed')",
        (now_ms(),))
    conn.commit()
    conn.close()
    assert db.entity_state(False, True, "tool", 1) == db.ENTITY_STATE_UNREPORTED

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        assert db.ENTITY_STATE_COLUMN not in headers(table), headers(table)
        assert first_column(table), "the fixture has uncatalogued tools to show"

        await activate(app, pilot, "tab-skills")
        skills = app.query_one("#t-skills", DataTable)
        at = list(tui.SKILLS_COLUMNS).index(db.ENTITY_STATE_COLUMN)
        row = first_column(skills).index("gone-skill")
        cell_value = skills.get_cell_at((row, at))
        assert str(cell_value) == db.ENTITY_STATE_REMOVED, cell_value
        # `Textual` has no per-row style hook, so the dimming rides the one cell.
        # Not `str` on purpose: a plain string would satisfy the text assertion
        # and lose the dimming without a word.
        assert isinstance(cell_value, tui.Text), type(cell_value)
        assert cell_value.style == "dim", cell_value.style
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_a_styled_cell_survives_the_name_cap():
    """`_cap_cell` used to be `(self, text: str) -> str` and measured with `len()`,
    so a renderable could not pass through it with its style intact. At `name_cap`
    below `len("installed")` the dimmed label is cut — and a cut that drops the style
    is the same silent lie as a silent column drop.
    """
    class _Host(tui.WideTableMixin):
        name_cap = 8

    host = _Host()
    clipped = host._cap_cell(tui.Text("installed", style="dim"))
    assert str(clipped) == "install…", str(clipped)
    assert isinstance(clipped, tui.Text) and clipped.style == "dim"
    # The same cut on an unstyled cell stays a plain string: the cap decides the
    # text, the incoming type decides the type.
    plain = host._cap_cell("installed")
    assert plain == "install…" and isinstance(plain, str)


# --- clipping and the row key ----------------------------------------------

def test_a_clipped_cell_still_keys_on_the_full_name(tracker):
    """`name_cap` is a display limit, never a row identity.

    A skill long enough to be clipped is real here (`basic-memory_*`-shaped names
    exceed 28 characters on any machine that uses MCP), and the drill-down must
    still open the right entity. Asserted against `db.q_skills` — the very query
    the panel fills from — so the key is compared with the name the database holds,
    not with another rendering of the cell.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row      # as the app opens it; q_skills names columns
    full = {r["skill"] for r in db.q_skills(conn)}
    conn.close()

    async def body(pilot, app):
        await activate(app, pilot, "tab-skills")
        # The long skill is installed but has no call in the fixture, so it lives
        # behind `a` now that the default list is used-in-window.
        await pilot.press("a")
        await pilot.pause(0.4)
        table = app.query_one("#t-skills", DataTable)
        names = first_column(table)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value
                for i in range(table.row_count)]
        keyed = [k.split("\t", 1)[1] for k in keys]
        # The precondition of this test, asserted rather than assumed: the terminal
        # is narrow enough that at least one name really was cut. When everything
        # fits, `_fill_wide` deliberately skips the cap and cells equal keys, so a
        # pass at that width would prove nothing about clipping.
        assert any(len(k) > len(c) for k, c in zip(keyed, names)), (keyed, names)
        assert all(k in full for k in keyed), [k for k in keyed if k not in full]
        # The cell is exactly the head of the key that fits — the display rule —
        # while the key itself stays the whole name the database holds. A skill
        # never contains "/", so this column is capped from the right by `_cap_cell`.
        assert names == [k if len(k) <= app.name_cap
                         else k[:app.name_cap - 1] + "…" for k in keyed], \
            (names, keyed)
        assert any(len(k) > app.name_cap for k in keyed), keyed
    drive(lambda: tui.TrackerApp(str(tracker), config={**db.DEFAULTS,
                                                       "name_cap": 12}), body,
          size=(60, 40))


# --- the network promise ---------------------------------------------------

def test_no_http_is_reachable_while_the_tui_paints(tracker, monkeypatch):
    """Same armed-door guard the previous TUI carried, re-pointed at this one.

    The claim is that the interface is local: nothing on the repaint path may open
    a socket. Three doors are armed, each is proven to answer once before the clean
    run is believed, and `socket.socket` itself is deliberately NOT patched —
    asyncio's selector loop builds its own self-pipe through it, so patching it
    makes the harness fail rather than the app, which is a different claim.
    """
    import http
    import socket
    import urllib.request

    hits = []

    def _blocked(name):
        def _fail(*a, **kw):
            hits.append(name)
            raise AssertionError(f"{name} called from the TUI")
        return _fail

    doors = (("urllib.request.urlopen", urllib.request, "urlopen",
              lambda: urllib.request.urlopen("http://127.0.0.1:1/armed")),
             ("http.client.HTTPConnection", http.client, "HTTPConnection",
              lambda: http.client.HTTPConnection("127.0.0.1", 1)),
             ("socket.create_connection", socket, "create_connection",
              lambda: socket.create_connection(("127.0.0.1", 1))))
    for _name, module, attr, _call in doors:
        monkeypatch.setattr(module, attr, _blocked(_name))
    for name, _m, _a, call in doors:          # arming proof first
        try:
            call()
        except AssertionError:
            pass
        assert hits == [name], f"{name} is not armed; the stub never answered: {hits}"
        hits.clear()

    async def body(pilot, app):
        for pane in TRACKER_TABS:
            await pilot.click(f"#{pane}")
            await pilot.pause(0.45)
        for key in ("s", "r"):
            await pilot.press(key)
            await pilot.pause(0.45)
        app.refresh_data()
        await pilot.pause(0.4)
    drive(lambda: tui.TrackerApp(str(tracker)), body)
    assert hits == [], hits


def test_the_sync_runs_in_a_daemon_thread_so_quitting_never_waits(tmp_path):
    """`q` must never block on an in-flight sync.

    Textual runs a thread worker on the event loop's default executor
    (`textual/worker.py`: ``loop.run_in_executor(None, …)``), and ``asyncio.run()``
    joins that executor before ``app.run()`` returns. So a sync still running when
    the user quits blocks the exit for as long as the sync takes — measured on this
    machine: ``r`` then ``q`` hung the app, and Ctrl+C produced a
    ``KeyboardInterrupt`` traceback out of ``shutdown_default_executor``. The sync
    therefore runs in a daemon thread, which the interpreter never joins.
    """
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    seen = {}

    class _RecordingSync:
        def run(self, **kwargs):
            t = threading.current_thread()
            seen["name"], seen["daemon"] = t.name, t.daemon
            return {"ok": True, "parts_read": 0}

    async def body(pilot, app):
        app.sync_mod = _RecordingSync()
        await pilot.press("r")
        for _ in range(400):
            if "name" in seen:
                break
            await asyncio.sleep(0.01)

    drive(lambda: tui.TrackerApp(str(out)), body)
    assert seen.get("name") == "ocut-sync", (
        "the sync did not run in the named daemon thread; it may be back on "
        f"Textual's default executor, which blocks quit: {seen!r}")
    assert seen.get("daemon") is True, (
        "the sync thread must be a daemon, or quitting waits for it")


def test_the_project_ledger_grows_with_its_rows_and_scrolls(tracker, clock):
    """The project board the sibling tracker cannot have — as a growing ledger.

    A cost-bearing step in its own project is inserted by hand, then read back
    off the board. The board used to be a fixed nine rows inside a fixed-height
    panel: a tenth project was silently invisible. It now takes the height its
    rows need and the dashboard pane scrolls once the screen is full.
    """
    conn = sqlite3.connect(tracker)
    ts = int(clock() * 1000) - 60_000
    conn.execute(
        "INSERT INTO model_calls(call_id, model, provider, project, directory,"
        " agent, variant, prompt_tokens, completion_tokens, total_tokens, cost,"
        " ts) VALUES('prt_mode', 'model-x', 'vendor', 'p_board', '/home/u/proj',"
        " 'build', 'max', 10, 5, 15, 0.0125, ?)", (ts,))
    conn.commit()

    async def body(pilot, app):
        app.tab_range["tab-dashboard"] = "all"
        app._refresh_active_tab()
        await pilot.pause(0.6)
        board = app.query_one("#dash-projects", DataTable)
        labels = [cell(board, i, 0) for i in range(board.row_count)]
        assert "proj" in labels, labels
        row = labels.index("proj")
        assert cell(board, row, 1) == "1", cell(board, row, 1)
        assert "$0.0125" in cell(board, row, 3), cell(board, row, 3)
        assert board.styles.height.unit is Unit.AUTO, board.styles.height
        pane = app.query_one("#dash-box")
        assert str(pane.styles.overflow_y) == "auto", pane.styles.overflow_y
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


# --- keyboard chrome: sort, filter, scroll and pushed screens --------------

def test_s_cycles_each_tabs_own_sort_order(tracker):
    """`s` replaces the old auto-sync toggle: mode 0 is query order, then the
    tab's declared fields, and the summary always names the order in force."""
    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        summary = app.query_one("#t-tools-summary")
        assert "sort: tab order" in str(summary.content)
        before = first_column(table)
        # tab order -> calls -> recent -> name -> fails -> tab order
        for expected in ("sort: calls", "sort: recent", "sort: name"):
            await pilot.press("s")
            await pilot.pause()
            assert expected in str(summary.content)
        assert first_column(table) == sorted(before, key=str.lower)
        for expected in ("sort: fails", "sort: tab order"):
            await pilot.press("s")
            await pilot.pause()
            assert expected in str(summary.content)
    drive(lambda: tui.TrackerApp(str(tracker)), body)

def test_a_live_filter_stays_visible_and_anchors_the_list_at_the_top(tracker):
    """A narrowed list starts at its first match, not at a deep old cursor.

    The filter row also stays visible after Enter: an applied filter the page
    hides is the state that used to make the rows look wrong for no reason.
    """
    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        inp = app.query_one("#filter")
        assert table.row_count >= 4, table.row_count
        table.move_cursor(row=table.row_count - 1)
        await pilot.pause()
        assert table.scroll_y > 0, (table.cursor_row, table.scroll_y)

        await pilot.press("/")
        await pilot.pause()
        assert inp.display and inp.has_focus
        for ch in "shell":
            await pilot.press(ch)
            await pilot.pause()
        assert first_column(table) == ["shell"], first_column(table)
        assert table.cursor_row == 0, table.cursor_row
        assert table.scroll_y == 0, table.scroll_y
        note = str(app.query_one("#t-tools-summary").content)
        assert 'filter "shell"' in note and "1 of" in note, note

        await pilot.press("enter")
        await pilot.pause()
        assert inp.display, "Enter hid a filter that is still applied"

        await pilot.press("escape")
        await pilot.pause()
        assert not inp.display
        assert table.row_count > 1, table.row_count
    drive(lambda: tui.TrackerApp(str(tracker)), body, size=(120, 10))

def test_the_main_screen_cannot_scroll_when_its_content_fits(tracker):
    """qcut's layout guarantee: the page never slides into blank rows.

    TabbedContent is constrained to `1fr`; without it the main screen measured
    two rows taller than the terminal and scrolled even on a quiet database.
    """
    async def body(pilot, app):
        from textual.widgets import TabbedContent
        assert app.screen.max_scroll_y == 0, app.screen.max_scroll_y
        assert app.query_one(TabbedContent).max_scroll_y == 0
        await activate(app, pilot, "tab-usage")
        assert app.screen.max_scroll_y == 0, app.screen.max_scroll_y
    drive(lambda: tui.TrackerApp(str(tracker)), body, size=(120, 20))

def test_a_pushed_detail_screen_carries_no_app_shortcuts(tracker):
    """Only the keys the detail screen itself binds reach the footer."""
    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        app.push_screen(tui.HistoryScreen("tool", "bash"))
        await pilot.pause()
        bindings = app.screen.active_bindings
        keys = set(bindings)
        assert any(key in keys for key in ("q", "escape", "escape,q")), keys
        leaked = [b.binding.action for b in bindings.values()
                  if b.binding.action in tui.TrackerApp.APP_SHORTCUT_ACTIONS]
        assert not leaked, leaked
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- what the status bar is allowed to shout about -------------------------

def test_the_status_line_separates_a_known_reason_from_a_new_one(tracker,
                                                                monkeypatch):
    """One total could not be read: it held a deadline and a format change both.

    `part_left_running` is this reader's own holdback deadline — a `tool` part the
    host never settled — so it is expected, and it must not raise the alarm the
    alarm exists for. A reason nothing here has been taught about must raise it, and
    must be named: the difference between "the format moved" and "a Tuesday" is the
    whole value of the line.

    The launch sync is disabled here: its fixture host is deliberately absent, and
    its failure would add a second, unrelated `!` to the line this case reads.
    """
    monkeypatch.setattr(tui.TrackerApp, "_request_sync", lambda self: None)
    conn = sqlite3.connect(tracker)
    conn.execute("DELETE FROM unparsed")
    conn.execute("INSERT INTO unparsed(reason, count) VALUES('part_left_running', 3)")
    conn.commit()
    conn.close()

    async def known(pilot, app):
        line = str(app.query_one("#status").content)
        assert "3 records unparsed (1 known)" in line, line
        assert "!" not in line, f"a known reason raised the alarm: {line}"

    drive(lambda: tui.TrackerApp(str(tracker)), known)

    conn = sqlite3.connect(tracker)
    conn.execute("INSERT INTO unparsed(reason, count)"
                 " VALUES('unknown_part_type:wat', 2)")
    conn.commit()
    conn.close()

    async def drift(pilot, app):
        line = str(app.query_one("#status").content)
        assert "!" in line, line
        assert "2 records unparsed under a new reason: unknown_part_type:wat" in line, line
        assert "3 records" not in line, (
            "the alarm is reporting the familiar records it would otherwise be "
            f"buried under: {line}")

    drive(lambda: tui.TrackerApp(str(tracker)), drift)
