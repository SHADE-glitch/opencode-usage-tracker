"""test_cross_repo_golden.py — one fixed input set, pinned against this repo's own code.

The Usage Total expression and the typed date/time entry were copied verbatim between
the three trackers in this family (cbut, ocut, qcut) and have been maintained separately
ever since; the NULL semantics already drifted once between all-usage-trackers and the
siblings (audit P1-12 / P2-17). The fix the audit recommends is not a rewrite into a
shared library — it is a golden fixture: one input set, run through each repo's own
code, with the expected results written down where a drift fails a test.

This file is the ocut half. It reads ``fixtures/cross_repo_golden.json`` — a byte-for-byte
copy of the canonical file in all-usage-trackers — builds the fixture rows through
``ocut_db``'s own schema, and asserts:

* every row's Usage Total through this repo's own ``_USAGE_TOTAL_EXPR``, and again
  through the TUI's Python helper ``_usage_total_tokens``, which is a second
  implementation of the same formula and therefore the likelier of the two to drift;
* every window preset's request count (from ``q_usage_summary``) and windowed Usage
  Total (the sum over ``q_usage_model_stats``, which is where this repo surfaces it);
* the per-model rollup — spellings stay separate and a NULL model is its own group;
* the typed date/time normalisation and the calendar-day window bounds;
* the same, plus end-to-end row aggregation, on both US DST transition days of 2026,
  with the process timezone switched to America/New_York.

The expected values follow this repo's documented definition (README M13: ``Input`` is
the uncached prompt plus both cache parts, and ``Usage Total`` is the display-only
``Input + Output + cache hit``; a partly-reported row still contributes, NULL only
without a completion or without any prompt part at all) — not the implementation. That
is the one documented difference from the two siblings, which return NULL as soon as
either token part is missing; the golden records each repo's own answer, so the
difference is pinned rather than smoothed over.

The TUI-helper cases need ``textual`` (a runtime dependency of the TUI); they skip
themselves without it, and the data-layer cases always run.
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "fixtures" / "cross_repo_golden.json"

#: Digest of the canonical copy in all-usage-trackers. A repo whose golden file has been
#: edited on its own fails here, with the digest it actually has in the message; the
#: update procedure (edit the canonical file, re-copy, update this constant in all three
#: repos) is written into the golden file itself.
CANONICAL_SHA256 = "375e115a63b29fcda567ad5d4a3ec6a02adeb940bbe796a8a1beb9db219eb975"

REPO = "ocut"
DST_ZONE = "America/New_York"


def load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def ms(iso):
    """A local wall-clock ISO string as epoch milliseconds, the unit every bound uses."""
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def golden_provider():
    """The one provider every fixture row carries, so rollup keys are stable."""
    return load_golden()["usage_total"]["provider"]


def ocut_columns(row):
    """The abstract row as this repo's columns.

    OpenCode reports an UNCACHED ``prompt_tokens`` with ``cache_read`` / ``cache_write``
    beside it, and its documented Input is all three added. So the abstract whole-prompt
    ``input_full`` is split as prompt = input_full - hit - write, with the cache parts
    stored as given. A NULL ``input_full`` leaves every prompt part NULL unless the row
    carried a cache part — which is exactly the u4 shape this repo answers differently
    from its siblings.
    """
    hit, write = row["cache_hit"], row["cache_write"]
    if row["input_full"] is None:
        prompt = None
    else:
        prompt = row["input_full"] - (hit or 0) - (write or 0)
    return prompt, row["output"], hit, write


def build_db(path, rows):
    """Insert golden rows into this repo's own table through its own column map."""
    conn = db.open_db(path)
    db.ensure_schema(conn)
    for row in rows:
        prompt, completion, hit, write = ocut_columns(row)
        conn.execute(
            "INSERT INTO model_calls(call_id, session_id, project, ts, model, provider,"
            " prompt_tokens, completion_tokens, cache_read, cache_write, usage_available)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], "s1", "/p", ms(row["ts"]), row["model"], golden_provider(),
             prompt, completion, hit, write, 1 if row["reports_usage"] else 0),
        )
    conn.commit()
    return conn


def row_totals(conn, golden):
    """{row id: Usage Total} through this repo's own per-row expression."""
    table = golden["repos"][REPO]["table"]
    key = golden["repos"][REPO]["key_column"]
    got = {}
    for r in conn.execute(
            f"SELECT {key} AS id, {db._USAGE_TOTAL_EXPR} AS usage_total"
            f" FROM {table}").fetchall():
        got[r["id"]] = r["usage_total"]
    return got


def windowed(conn, key, now_ms):
    """(requests, windowed Usage Total) for one preset, through this repo's queries.

    ``q_usage_summary`` deliberately does not surface Usage Total here, so the windowed
    total is the sum over the per-model rollup — the same rows, the same window, the
    same expression.
    """
    start, end = db.window_bounds(key, now_ms)
    requests = db.q_usage_summary(conn, start, end)["requests"]
    stats = db.q_usage_model_stats(conn, start, end)
    total = sum(r["usage_total_tokens"] for r in stats if r["usage_total_tokens"] is not None)
    return requests, total


def model_rollup(conn, now_ms):
    """{(model, provider): usage_total} through this repo's own rollup query."""
    start, end = db.window_bounds("all", now_ms)
    return {(r["model"], r["provider"]): r["usage_total_tokens"]
            for r in db.q_usage_model_stats(conn, start, end)}


@pytest.fixture(scope="module")
def golden():
    return load_golden()


@pytest.fixture(scope="module")
def conn(golden, tmp_path_factory):
    connection = build_db(tmp_path_factory.mktemp("golden") / "usage.db",
                          golden["usage_total"]["rows"])
    yield connection
    connection.close()


def test_the_copy_is_the_canonical_one():
    """A golden file edited in only one repo is the drift this fixture exists to catch.

    Each repo's CI checks out only itself, so the copies cannot read one shared file
    through a sibling-relative path — that would be red on every runner. The digest is
    what keeps the copies one document instead of three.
    """
    actual = hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest()
    assert actual == CANONICAL_SHA256, (
        "this repo's copy of cross_repo_golden.json is not the canonical one. Edit the "
        "canonical copy in all-usage-trackers, re-copy it here, and update "
        "CANONICAL_SHA256 in this file (and in the other two repos' copies of this "
        "test) in the same change.")


def test_the_family_preset_table_is_shared():
    """USAGE_RANGES is the one table all four repositories copy verbatim.

    A preset added here and not on the board (or the reverse) makes "7d" mean two
    different things, and neither repository fails on its own. The table lives in
    the fixture so the four copies cannot drift quietly.
    """
    assert db.USAGE_RANGES == load_golden()["family_contract"]["ranges"]


@pytest.mark.parametrize("case", load_golden()["usage_total"]["rows"],
                         ids=[r["id"] for r in load_golden()["usage_total"]["rows"]])
def test_every_row_matches_the_golden_usage_total(conn, golden, case):
    """One case per NULL/partial/cache/spelling shape, through _USAGE_TOTAL_EXPR.

    The interesting one is u4 — a missing prompt with a present output: this repo's
    documented definition keeps the partly-reported row (180), while both siblings
    return NULL. Both are pinned; neither is smoothed.
    """
    got = row_totals(conn, golden)
    assert got[case["id"]] == case["expect"][REPO], case["meaning"]


@pytest.mark.parametrize("case", load_golden()["usage_total"]["rows"],
                         ids=[r["id"] for r in load_golden()["usage_total"]["rows"]])
def test_the_tui_helper_agrees_with_the_sql_expression(case):
    """The TUI's Python half of Usage Total must equal the SQL half, row for row.

    ``_usage_total_tokens`` re-implements ``_USAGE_TOTAL_EXPR`` in Python for the cells
    the rollup query does not cover. Two implementations of one number is exactly the
    shape that drifts, so both are pinned against the same golden.
    """
    pytest.importorskip("textual")
    spec = importlib.util.spec_from_file_location(
        "ocut_tui_under_test",
        str(Path(__file__).resolve().parents[1] / "ocut-tui.py"))
    tui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tui)
    prompt, completion, hit, write = ocut_columns(case)
    assert tui._usage_total_tokens(prompt, completion, hit, write) == case["expect"][REPO], (
        case["meaning"])


@pytest.mark.parametrize("case", load_golden()["usage_total"]["windows"],
                         ids=[c["key"] for c in load_golden()["usage_total"]["windows"]])
def test_the_window_aggregates_match(conn, golden, case):
    """Every preset's requests and windowed Usage Total.

    w1 sits at exactly ``now``: the window end is exclusive, so it is outside every
    preset — all-time included. w3 and w7 sit exactly on a local midnight: the start
    is inclusive. That pair is the whole boundary contract in two rows.
    """
    now = ms(golden["usage_total"]["now"])
    requests, total = windowed(conn, case["key"], now)
    assert requests == case["requests"]
    assert total == case["usage_total"][REPO]


def test_the_model_rollup_keeps_spellings_and_null_separate(conn, golden):
    """The rollup groups by (model, provider) exactly: no case folding, no merge.

    ``step-3`` and ``Step-3`` are two rows, and the NULL model is its own group with
    its tokens intact — a group that quietly disappears from the Tokens tab is the
    same class of bug as a fabricated 0.
    """
    now = ms(golden["usage_total"]["now"])
    got = model_rollup(conn, now)
    want = {(g["model"], g["provider"]): g["usage_total"][REPO]
            for g in golden["usage_total"]["model_groups"]}
    assert got == want


@pytest.mark.parametrize("case", load_golden()["window_bounds"]["cases"],
                         ids=[c["key"] for c in load_golden()["window_bounds"]["cases"]])
def test_the_preset_bounds(golden, case):
    """window_bounds: whole local calendar days, ending exactly at now."""
    section = golden["window_bounds"]
    now = ms(section["now"])
    start, end = db.window_bounds(case["key"], now)
    assert end == now
    if case["start"] is None:
        assert start is None, "only 'all' has no lower bound"
    else:
        assert start == ms(case["start"])


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_date"]
             if c["expect"] != "ValueError"],
    ids=[c["input"] for c in load_golden()["date_parsing"]["parse_date"]
         if c["expect"] != "ValueError"])
def test_parse_date_cases(golden, case):
    section = golden["date_parsing"]
    year = case.get("default_year", section["default_year"])
    assert db.parse_date(case["input"], year).date().isoformat() == case["expect"]


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_date"]
             if c["expect"] == "ValueError"],
    ids=[c["input"] for c in load_golden()["date_parsing"]["parse_date"]
         if c["expect"] == "ValueError"])
def test_parse_date_rejects_what_it_cannot_read(golden, case):
    section = golden["date_parsing"]
    year = case.get("default_year", section["default_year"])
    with pytest.raises(ValueError):
        db.parse_date(case["input"], year)


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_time"]
             if c["expect"] != "ValueError"],
    ids=[c["input"] for c in load_golden()["date_parsing"]["parse_time"]
         if c["expect"] != "ValueError"])
def test_parse_time_cases(golden, case):
    assert list(db.parse_time(case["input"])) == case["expect"]


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_time"]
             if c["expect"] == "ValueError"],
    ids=[c["input"] for c in load_golden()["date_parsing"]["parse_time"]
         if c["expect"] == "ValueError"])
def test_parse_time_rejects_what_it_cannot_read(case):
    with pytest.raises(ValueError):
        db.parse_time(case["input"])


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_datetime"]
             if c["expect"] not in (None, "ValueError")],
    ids=[f"{c['date']}|{c['time']}" for c in load_golden()["date_parsing"]["parse_datetime"]
         if c["expect"] not in (None, "ValueError")])
def test_parse_datetime_cases(golden, case):
    section = golden["date_parsing"]
    year = case.get("default_year", section["default_year"])
    assert db.parse_datetime(case["date"], case["time"], year) == ms(case["expect"])


@pytest.mark.parametrize(
    "case", [c for c in load_golden()["date_parsing"]["parse_datetime"]
             if c["expect"] in (None, "ValueError")],
    ids=[f"{c['date']}|{c['time']}" for c in load_golden()["date_parsing"]["parse_datetime"]
         if c["expect"] in (None, "ValueError")])
def test_parse_datetime_blank_date_is_none_and_errors_raise(golden, case):
    section = golden["date_parsing"]
    year = case.get("default_year", section["default_year"])
    if case["expect"] is None:
        assert db.parse_datetime(case["date"], case["time"], year) is None
    else:
        with pytest.raises(ValueError):
            db.parse_datetime(case["date"], case["time"], year)


# --- the DST transition days ------------------------------------------------
# "2 days back" must land on local midnight two calendar days ago, not on now minus 48
# hours: on the spring-forward day those differ by eleven hours, and a window that
# silently became hour-based would include or drop a whole day of calls. The row
# aggregation is re-run inside the switched zone, because that is where a local-day
# boundary is actually decided.

def _zone_available():
    try:
        ZoneInfo(DST_ZONE)
    except Exception:
        return False
    return True


needs_tz = pytest.mark.skipif(
    not hasattr(time, "tzset") or not _zone_available(),
    reason="this platform cannot switch timezones or has no tzdata")


class in_timezone:
    """Run a block with the process timezone switched, then put it back exactly."""

    def __init__(self, name):
        self.name = name
        self.saved = os.environ.get("TZ")

    def __enter__(self):
        os.environ["TZ"] = self.name
        time.tzset()
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.saved
        time.tzset()
        return False


@needs_tz
@pytest.mark.parametrize("which", ["spring_forward", "fall_back"])
def test_the_transition_day_window_bounds(golden, which):
    section = golden["dst"][which]
    with in_timezone(DST_ZONE):
        now = ms(section["now"])
        for case in section["window_bounds"]:
            start, end = db.window_bounds(case["key"], now)
            assert end == now
            assert start == ms(case["start"]), case["key"]


@needs_tz
@pytest.mark.parametrize("which", ["spring_forward", "fall_back"])
def test_the_transition_day_rows_stay_in_the_local_day(golden, which, tmp_path):
    """The rows around the jump are the same local day; the row at now stays outside.

    Spring: d2 (01:30 EST) and d3 (03:30 EDT) share one local day across the jump.
    Fall: d6 is the first pass of the repeated 01:30 hour (fold=0, EDT). In both cases
    the row at exactly ``now`` is outside, because the window end is exclusive.
    """
    section = golden["dst"][which]
    with in_timezone(DST_ZONE):
        conn = build_db(tmp_path / "usage.db", section["rows"])
        try:
            now = ms(section["now"])
            for case in section["windows"]:
                requests, total = windowed(conn, case["key"], now)
                assert requests == case["requests"], case["key"]
                assert total == case["usage_total"][REPO], case["key"]
        finally:
            conn.close()


@needs_tz
@pytest.mark.parametrize(
    "case", load_golden()["dst"]["ambiguous_hour"],
    ids=[f"{c['date']}|{c['time']}" for c in load_golden()["dst"]["ambiguous_hour"]])
def test_the_ambiguous_hour_resolves_like_the_stdlib(golden, case):
    """The gap hour and the repeated hour resolve exactly like datetime.timestamp().

    parse_datetime is built on a naive ``datetime.timestamp()``, so the pre-jump 01:30
    is EST, the post-jump 03:30 is EDT, and November's repeated 01:30 is the first pass
    (fold=0, EDT). A hand-rolled offset table is where this drifts.
    """
    with in_timezone(DST_ZONE):
        got = db.parse_datetime(case["date"], case["time"],
                                golden["date_parsing"]["default_year"])
        assert got == ms(case["expect"])
