"""Tests for the `state` column every entity row now carries.

The panels list the union of what is installed (`inventory`, rewritten on every
sync) and what has ever been used (the usage tables). Before this, that `UNION`
erased the one piece of evidence that separates a name still on disk from one
that is gone: an installed-and-unused entity and an uninstalled one whose calls
stayed both rendered as a row with a zero count, and the only difference was a
`-` where `last used` should be. A user's reading of that is "I deleted it, why
is it still listed", and the database had no way to answer it either.

These cases pin the three states and the two flags they are derived from, and
they are deliberately read back through the real `q_*` functions rather than
through the values a case just inserted, so a flag that stops reaching a panel
fails here.
"""

import subprocess
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "ocut_state_tui", str(SCRIPTS / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)


# --- fixture ----------------------------------------------------------------

# kind -> (query, the row field that names it). MCP is keyed by (server, tool)
# and the others by a single name, which is why the lookup below special-cases it.
_QUERY = {
    "tool": (db.q_tools, "tool_name"),
    "skill": (db.q_skills, "skill"),
    "agent": (db.q_agents, "agent_type"),
    "plugin": (db.q_plugins, "plugin"),
    "mcp": (db.q_mcp, "server"),
}


def _table(kind: str) -> tuple[str, str]:
    """The usage table and name column behind one entity kind."""
    return {
        "tool": ("tool_calls", "tool_name"),
        "skill": ("skill_usage", "skill"),
        "agent": ("agent_usage", "agent_type"),
        "plugin": ("plugin_usage", "plugin"),
        "mcp": ("mcp_usage", "server"),
    }[kind]


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "state.db")
    db.ensure_schema(c)
    yield c
    c.close()


def install(c, kind, name, owner="", version=None):
    c.execute(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version, path,"
        " source) VALUES(?,?,?,?,NULL,'fixture')", (kind, name, owner, version))


def used(c, kind, name, ts=1_700_000_000_000, n=1):
    """Record `n` calls against a name, the way the reader would."""
    table, column = _table(kind)
    cols = [f"call_id", column]
    row = [f"{kind}-{name}-{ts}-0", name]
    if kind == "mcp":
        cols.append("tool")
        row.append(f"{name}_tool")
    cols += ["session_id", "ts"]
    row += ["ses_1", ts]
    for i in range(n):
        row[0] = f"{kind}-{name}-{ts}-{i}"
        c.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)})"
                  f" VALUES({','.join('?' * len(cols))})", row)
    c.commit()


def model_use(c, model, provider, call_id=None, ts=1_700_000_000_000):
    """One step that names a model and the provider that served it."""
    c.execute(
        "INSERT INTO model_calls(call_id, model, provider, prompt_tokens,"
        " completion_tokens, total_tokens, ts) VALUES(?,?,?,10,5,15,?)",
        (call_id or f"model-{model}-{provider}-{ts}", model, provider, ts))
    c.commit()


def states(c, kind):
    """{name: label} for every row the panel would show.

    The kind is passed to the label rather than looked up from the row: it is
    the thing that decides the label, so a helper that inferred it would be
    assuming the answer.
    """
    query, key = _QUERY[kind]
    return {r[key]: db.entity_state(r["installed"], r["ever_used"], kind)
            for r in query(c)}


# --- the states --------------------------------------------------------------

# Which kinds the reader can actually take a census of: there has to be a file
# to read or a config key to name. `tool` and `agent` have neither -- the host
# compiles its builtins into its own binary -- so a missing row for those means
# "we did not look", never "it is gone".
_CENSUS = {k for k, ok in db.ENTITY_CENSUS.items() if ok}


def test_the_kinds_without_a_census_are_the_ones_this_reader_cannot_enumerate():
    """Pinned so the map is not edited to make a test pass. A kind joins the
    census only when this repository gains a way to read its installed set.
    """
    assert _CENSUS == {"skill", "plugin", "mcp"}
    assert db.ENTITY_CENSUS["tool"] is False and db.ENTITY_CENSUS["agent"] is False


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_a_name_with_history_and_no_inventory_row_reads_by_what_we_could_know(conn, kind):
    """`inventory` is a snapshot of what is installed *now*, so a missing row is
    the only evidence of removal -- where there is a census to take. For a kind
    with no census the same missing row means something weaker, and reporting it
    as `removed` is how every builtin `bash` ended up labelled gone on a machine
    where it has been called twelve thousand times.
    """
    used(conn, kind, "gone")
    expected = (db.ENTITY_STATE_REMOVED if kind in _CENSUS
                else db.ENTITY_STATE_UNREPORTED)
    assert states(conn, kind)["gone"] == expected


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_an_installed_entity_is_installed_whatever_the_census(conn, kind):
    """The census only ever weakens the *negative* answer. A name the inventory
    does carry is a name somebody put there on purpose, and no gap in what we
    can enumerate should turn it into `not reported`.
    """
    install(conn, kind, "fresh")
    assert states(conn, kind)["fresh"] == db.ENTITY_STATE_INSTALLED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_an_installed_entity_that_was_never_called_is_installed(conn, kind):
    install(conn, kind, "fresh")
    assert states(conn, kind)["fresh"] == db.ENTITY_STATE_INSTALLED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_a_live_entity_in_use_is_in_use(conn, kind):
    install(conn, kind, "live")
    used(conn, kind, "live")
    assert states(conn, kind)["live"] == db.ENTITY_STATE_USED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_removing_an_entity_does_not_remove_its_history(conn, kind):
    """`installed` is what changes and only that: the row keeps its counts, which
    is the whole point of a union -- a deleted skill's history is the only record
    that it existed.
    """
    install(conn, kind, "was-here")
    used(conn, kind, "was-here", n=3)
    before = _QUERY[kind][0](conn)[0]["calls" if kind != "plugin" else "uses"]
    conn.execute("DELETE FROM inventory")
    conn.commit()
    expected = (db.ENTITY_STATE_REMOVED if kind in _CENSUS
                else db.ENTITY_STATE_UNREPORTED)
    assert states(conn, kind)["was-here"] == expected
    after = _QUERY[kind][0](conn)[0]["calls" if kind != "plugin" else "uses"]
    assert after == before == 3


def test_the_state_follows_the_window_when_a_count_is_passed(conn):
    """The label agrees with the row it sits on.

    This reverses an earlier rule on purpose: `ever_used` comes from an
    un-windowed scan, so a panel narrowed to a day printed "in use" beside a 0
    and a person reading the row could not tell which cell to believe. The
    windowed count decides between `in use` and `installed`; a caller with no
    count (the all-time headless reports) still gets the ever-used answer, so
    both surfaces agree when they are asked the same question.
    """
    install(conn, "tool", "yesterday")
    used(conn, "tool", "yesterday", ts=1_700_000_000_000)
    windowed = db.q_tools(conn, start_ts=1_900_000_000_000)[0]
    assert windowed["calls"] == 0, "the window should have emptied the count"
    assert db.entity_state(windowed["installed"], windowed["ever_used"], "tool",
                           windowed["calls"]) == db.ENTITY_STATE_INSTALLED
    alltime = db.q_tools(conn)[0]
    assert db.entity_state(alltime["installed"], alltime["ever_used"], "tool",
                           alltime["calls"]) == db.ENTITY_STATE_USED
    # No count -> the flag answers, which is the headless all-time reading.
    assert db.entity_state(1, 1, "tool") == db.ENTITY_STATE_USED


def test_the_window_never_relabels_a_name_that_is_gone_or_unreported(conn):
    """Only the choice between two live labels follows the window. `removed` and
    `not reported` are decided from the inventory first, so a retired skill with
    older calls does not become "installed" the moment the range narrows, and a
    builtin tool with no census does not become one either.
    """
    used(conn, "skill", "retired", ts=1_700_000_000_000)
    assert db.entity_state(0, 1, "skill", 0) == db.ENTITY_STATE_REMOVED
    assert db.entity_state(0, 1, "tool", 0) == db.ENTITY_STATE_UNREPORTED
    assert db.entity_state(0, 0, "tool", 0) == db.ENTITY_STATE_UNREPORTED


def test_a_control_character_in_a_name_cannot_break_a_cell(conn):
    """A host-derived name can carry a newline (a malformed model call did). A
    newline in a DataTable cell renders as a second line and doubles that row's
    height, and the same bytes move the cursor in a terminal report. The stored
    value is left alone; what is painted goes through `clean_cell`.
    """
    assert db.clean_cell("REPO<\n<parameter name=") == "REPO< <parameter name="
    assert db.clean_cell("a\x1b[31mb") == "a [31mb"
    assert db.clean_cell(None) == "-"
    assert db.clean_cell(12) == "12"
    assert "\n" not in db.clean_cell("two\nlines")


def test_removed_outranks_in_use(conn):
    """A name that is not on disk is not "in use" even though its history says
    otherwise: that half of the label is the one a person can act on.
    """
    used(conn, "skill", "uninstalled-skill", n=5)
    assert db.entity_state(0, 1, "skill") == db.ENTITY_STATE_REMOVED
    assert db.entity_state(1, 0, "skill") == db.ENTITY_STATE_INSTALLED
    assert db.entity_state(1, 1, "skill") == db.ENTITY_STATE_USED


def test_a_kind_without_a_census_says_so_instead_of_guessing(conn):
    """The whole point of the fourth label. `bash` has no file to be found and no
    config key that names it, so a tool row with no inventory row is missing
    evidence, not evidence of absence -- and the two are not interchangeable when
    the reader's job is to be believed.
    """
    assert db.entity_state(0, 1, "tool") == db.ENTITY_STATE_UNREPORTED
    assert db.entity_state(0, 0, "agent") == db.ENTITY_STATE_UNREPORTED
    assert db.entity_state(1, 1, "tool") == db.ENTITY_STATE_USED
    # A kind nobody declared has no census either. Guessing one would be the
    # failure this map exists to prevent, so an unknown kind is treated as blind.
    assert db.entity_state(0, 0, "nonesuch") == db.ENTITY_STATE_UNREPORTED


def test_the_label_requires_a_kind():
    """No default. A front end that forgot the argument would render the old
    three-state label for every row, the row would still carry a plausible word,
    and no assertion in the suite would notice.
    """
    with pytest.raises(TypeError):
        db.entity_state(0, 1)


# --- the flags reach the rows -------------------------------------------------

def test_a_name_on_both_sides_is_counted_once(conn):
    """Regression: projecting the flags onto the raw UNION instead of aggregating
    them leaves an installed-and-used name with two rows in the names CTE, and
    the LEFT JOIN then counts its usage once per row -- two plugin uses read as
    four.
    """
    install(conn, "plugin", "alpha")
    used(conn, "plugin", "alpha", n=2)
    install(conn, "tool", "bash")
    used(conn, "tool", "bash", n=2)
    row = db.q_plugins(conn)[0]
    assert (row["uses"], row["installed"], row["ever_used"]) == (2, 1, 1)
    assert db.q_tools(conn)[0]["calls"] == 2


def test_mcp_rows_inherit_their_servers_install_state(conn):
    """MCP is keyed by (server, tool) but the config declares servers, so the
    lookup cannot be a flag carried through the UNION -- and a server that was
    configured and never called must not be relabelled "in use" by the flag being
    hard-wired.
    """
    install(conn, "mcp", "live-server")
    install(conn, "mcp", "idle-server")
    used(conn, "mcp", "live-server")
    used(conn, "mcp", "retired-server")
    rows = {r["server"]: db.entity_state(r["installed"], r["ever_used"], "mcp")
            for r in db.q_mcp(conn)}
    assert rows["live-server"] == db.ENTITY_STATE_USED
    assert rows["idle-server"] == db.ENTITY_STATE_INSTALLED
    assert rows["retired-server"] == db.ENTITY_STATE_REMOVED
    idle = [r for r in db.q_mcp(conn) if r["server"] == "idle-server"][0]
    assert idle["tool"] is None, "a configured-but-unused server lists once"


def test_a_plugin_known_only_as_an_owner_is_not_installed(conn):
    """`owner_plugin` names a plugin that ships a tool without having a
    `kind='plugin'` row. It is not evidence of installation -- only the host's own
    config says a plugin is installed -- so this arm contributes `installed = 0`
    rather than borrowing the owner's.
    """
    install(conn, "tool", "plugin-tool", owner="orphan")
    install(conn, "plugin", "declared")
    rows = {r["plugin"]: db.entity_state(r["installed"], r["ever_used"], "plugin")
            for r in db.q_plugins(conn)}
    assert rows["orphan"] == db.ENTITY_STATE_REMOVED
    assert rows["declared"] == db.ENTITY_STATE_INSTALLED


def test_mcp_and_provider_owners_never_surface_as_plugins(conn):
    """Regression: three of the four rows on this host's Plugins tab were not
    plugins. An MCP tool's `owner_plugin` is its server and a model's is its
    provider -- neither is evidence that something called `basic-memory` or
    `huoshan` was ever installed as a plugin, so both arms named things that had
    only ever been owners and labelled them `removed`.
    """
    # `install` stamps source='fixture'; these two rows are written the way the
    # reader writes them -- an MCP tool's row is sourced 'mcp', a model's is its
    # catalogue entry with the provider in `owner_plugin`.
    conn.execute(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version,"
        " path, source) VALUES('tool', 'basic-memory_search_notes',"
        " 'basic-memory', NULL, NULL, 'mcp')")
    conn.execute(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version,"
        " path, source) VALUES('model', 'kimi-k2', 'huoshan', NULL, NULL,"
        " 'models-dev')")
    install(conn, "plugin", "real-plugin")
    names = {r["plugin"] for r in db.q_plugins(conn)}
    assert names == {"real-plugin"}, names

def test_the_state_is_derived_not_stored(conn):
    """No column anywhere: the two flags are what `inventory` and the usage
    tables already say, and a third copy could only ever disagree with them.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inventory)")}
    assert "installed" not in cols and "ever_used" not in cols
    assert db.ENTITY_STATE_COLUMN == "state"


# --- the views ---------------------------------------------------------------

@pytest.mark.parametrize("view", ["v_tools", "v_skills", "v_agents", "v_mcp",
                                 "v_plugins"])
def test_every_entity_view_carries_the_two_flags(conn, view):
    """`ocut export` reads the views, not the queries. A flag that reached the
    panels but not the views would make the export silently disagree with the
    screen.
    """
    cols = [c[0] for c in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]
    assert cols[-2:] == ["installed", "ever_used"], cols


def test_the_export_views_agree_with_the_queries(conn):
    install(conn, "skill", "tdd")
    used(conn, "skill", "tdd", n=2)
    used(conn, "skill", "retired")
    q = {r["skill"]: (r["calls"], r["installed"], r["ever_used"])
         for r in db.q_skills(conn)}
    v = {r["skill"]: (r["calls"], r["installed"], r["ever_used"])
         for r in conn.execute("SELECT * FROM v_skills")}
    assert q == v


def test_v_plugins_counts_the_four_kinds_a_plugin_can_own(conn):
    """It counted two of the four. A plugin that ships a skill but no tool was
    reported as shipping nothing, and nothing in this repository says the view is
    only about tools -- `q_plugins` has always counted all four.
    """
    install(conn, "tool", "t1", owner="alpha")
    install(conn, "command", "c1", owner="alpha")
    install(conn, "skill", "s1", owner="alpha")
    install(conn, "agent", "a1", owner="alpha")
    row = conn.execute("SELECT * FROM v_plugins WHERE plugin='alpha'").fetchone()
    assert (row["tools"], row["commands"], row["skills"], row["agents"]) == (1, 1, 1, 1)
    q = {r["plugin"]: r for r in db.q_plugins(conn)}["alpha"]
    assert (q["tools"], q["commands"], q["skills"], q["agents"]) == (1, 1, 1, 1)

# --- the headless surface ---------------------------------------------------

# --- the OpenCode-only dimensions -------------------------------------------

def tool_use(c, name, ts=1_700_000_000_000, n=1, status="completed"):
    """Raw tool_calls rows under the spelling the host wrote — no aliasing."""
    for i in range(n):
        c.execute(
            "INSERT OR REPLACE INTO tool_calls(call_id, tool_name, category,"
            " status, ts) VALUES(?,?,?,?,?)",
            (f"tool-{name}-{ts}-{i}", name, "builtin", status, ts))
    c.commit()


def step_use(c, call_id, agent="build", variant="max", cost=None,
             ts=1_700_000_000_000, model="model-x", provider="vendor"):
    """One model step under one (agent, variant) pair, the way the host writes it."""
    c.execute(
        "INSERT OR REPLACE INTO model_calls(call_id, model, provider, agent,"
        " variant, prompt_tokens, completion_tokens, cache_read, cache_write,"
        " cost, ts, usage_available) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
        (call_id, model, provider, agent, variant, 10, 5, 2, 1, cost, ts))
    c.commit()


def session_row(c, sid, *, started=1_700_000_000_000, ended=None,
                agent="build", is_subagent=0, directory="/home/u/proj",
                tokens=(100, 20, 0, 30, 5)):
    c.execute(
        "INSERT OR REPLACE INTO sessions(session_id, project, directory, agent,"
        " model, provider, parent_id, is_subagent, started_at, ended_at, cost,"
        " tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,"
        " tokens_cache_write, host_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "p1", directory, agent, "model-x", "vendor", None, is_subagent,
         started, ended, 0.0, *tokens, "2.0.32"))
    c.commit()


def test_a_renamed_tool_is_one_row_keyed_by_its_current_name(conn):
    """v1's `bash` is v2's `shell` (TOOL_CANONICAL): one panel row, both histories.

    Two spellings of one tool read as two tools — the older one looks retired
    while the newer looks new. The merge keys the row by the CURRENT name, the
    raw `tool_calls` rows keep the spelling the host wrote, and a drill-down
    opened from either spelling returns both.
    """
    tool_use(conn, "bash", ts=1_700_000_000_000, n=3)
    tool_use(conn, "shell", ts=1_700_000_100_000, n=2)
    rows = db.q_tools(conn)
    assert [(r["tool_name"], r["calls"]) for r in rows] == [("shell", 5)]
    v = [tuple(r)[:2] for r in conn.execute(
        "SELECT tool_name, calls FROM v_tools")]
    assert v == [("shell", 5)]
    for spelling in ("bash", "shell"):
        assert len(db.q_history(conn, "tool", spelling, limit=50)) == 5, spelling
    # The raw record is untouched: the single-call detail still shows which
    # spelling the host wrote.
    raw = {r[0] for r in conn.execute("SELECT tool_name FROM tool_calls")}
    assert raw == {"bash", "shell"}


def test_mode_stats_group_steps_by_the_host_agent(conn):
    """`model_calls.agent` answers "where did the tokens go" — grouped, not lost.

    A subagent's steps carry the subagent's name; the session modes carry
    theirs. Cost orders the table (it is a money question) and an unknown
    grouping is refused, never silently treated as a column.
    """
    step_use(conn, "s1", agent="build", cost=0.5)
    step_use(conn, "s2", agent="build", cost=0.25)
    step_use(conn, "s3", agent="general", cost=0.0)
    step_use(conn, "s4", agent=None)
    rows = db.q_mode_stats(conn, by="agent")
    assert [(r["mode"], r["steps"], r["cost"]) for r in rows] == [
        ("build", 2, 0.75), ("general", 1, 0.0), ("-", 1, None)]
    var = db.q_mode_stats(conn, by="variant")
    assert [r["mode"] for r in var] == ["max"]
    assert var[0]["steps"] == 4
    with pytest.raises(ValueError):
        db.q_mode_stats(conn, by="model_id")


def test_sessions_report_lists_newest_first_with_the_host_rollup(conn):
    session_row(conn, "ses_old", started=1_700_000_000_000,
                ended=1_700_000_060_000)
    session_row(conn, "ses_new", started=1_700_000_900_000,
                ended=1_700_000_990_000, is_subagent=1, agent="general")
    rows = db.q_sessions(conn)
    assert [r["session_id"] for r in rows] == ["ses_new", "ses_old"]
    assert rows[0]["duration_ms"] == 90_000
    assert rows[0]["is_subagent"] == 1

PLUGIN_HEADERS = ["plugin", "uses", "tools", "skills", "agents", "cmds",
                  "last used", "state"]


def stats(conn, *args) -> str:
    """`ocut <args>` against this case's own database, as a person would run it.

    The commit is load-bearing: this is a second process reading the file, and
    `sqlite3` holds this case's inserts in an open transaction until something says
    otherwise. Without it the child sees a schema and no rows, and the case fails on
    "(none)" — a real symptom of the wrong thing.
    """
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.commit()
    r = subprocess.run([sys.executable, str(SCRIPTS / "ocut-stats.py"),
                        "--db", str(path), *args],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def header_line(out: str) -> list[str]:
    """The Plugins header, found by its own separator rule rather than by position.

    A two-word label ("last used") is one column, so token-counting is wrong and
    line-offset is wrong; the line whose every field after the first is a run of
    dashes is the one thing in this output that cannot also be a data row.
    """
    lines = out.splitlines()
    for i, line in enumerate(lines[1:], start=1):
        fields = line.split()
        if len(fields) > 1 and all(set(f) <= {"-"} for f in fields[1:]):
            return lines[i - 1].split()
    raise AssertionError(f"no Plugins header in:\n{out}")


def test_the_headless_plugins_list_is_column_for_column_the_tui(conn):
    """`cmd_plugins` says in its own comment that it prints the same fields, in the
    same order, as the Plugins tab, "so the two surfaces cannot disagree about what
    is known". That sentence was false before either side carried `state`, and it
    would stay false the moment one gained a column the other did not -- so it is
    compared against the tuple rather than trusted.

    Compared after collapsing runs of spaces: the headless side pads to column
    width, so the padding is the one thing that legitimately differs.
    """
    install(conn, "plugin", "alpha", version="1.2.3")
    install(conn, "skill", "s1", owner="alpha")
    out = stats(conn, "plugins")
    # A two-word label ("last used") is one column, so both sides are compared
    # after collapsing whitespace rather than token by token.
    assert " ".join(header_line(out)) == " ".join(" ".join(PLUGIN_HEADERS).split()), out
    assert " ".join(PLUGIN_HEADERS) == " ".join(tui.PLUGINS_COLUMNS), "drifted"


def test_the_headless_plugins_list_prints_a_state_per_row(conn):
    """The count alone cannot say whether a plugin is still there, which is what
    the column added — and it must reach the headless surface too, or a user who
    scripts `ocut plugins` gets the older, less honest answer than the one in the TUI.
    """
    install(conn, "plugin", "alpha", version="1.2.3")
    install(conn, "skill", "s1", owner="alpha")
    used(conn, "plugin", "retired")
    body = [ln for ln in stats(conn, "plugins").splitlines()
            if ln.split() and ln.split()[0] in ("alpha", "retired")]
    assert len(body) == 2, body
    printed = {ln.split()[0]: ln.split()[-1] for ln in body}
    assert printed["alpha"] == db.ENTITY_STATE_INSTALLED, body
    assert printed["retired"] == db.ENTITY_STATE_REMOVED, body

# --- the Tokens panel lists what answered, not what is configured -----------

def test_a_configured_model_with_no_requests_gets_no_row(conn):
    """A model the host's config offers is a catalogue entry, not a usage row.

    It has no request, no token and no route claim to make, so listing it only
    produced zero rows for models nobody had called. The usage side still lists
    a model that *has* answered, exactly once.
    """
    install(conn, "model", "ark-code-latest", owner="huoshan")
    install(conn, "model", "used-model", owner="opencode")
    conn.execute(
        "INSERT INTO model_calls(call_id, model, provider, prompt_tokens,"
        " completion_tokens, total_tokens, ts) VALUES('c1','used-model',"
        "'opencode',10,5,15,1700000000000)")
    conn.commit()
    rows = {r["model"]: r for r in db.q_model_tokens(conn)}
    assert sorted(rows) == ["used-model"], sorted(rows)
    assert rows["used-model"]["responses"] == 1
    assert rows["used-model"]["total_tokens"] == 15


def test_a_model_that_left_the_config_keeps_its_history(conn):
    """The config is a snapshot; the steps it produced are the record.

    A model removed from the host config must keep the totals its own steps
    earned, or yesterday's numbers would move when the config changes.
    """
    model_use(conn, "retired-model", "huoshan")
    rows = {r["model"]: r for r in db.q_model_tokens(conn)}
    assert rows["retired-model"]["responses"] == 1


def test_one_model_on_two_providers_is_two_rows(conn):
    """The row key carries the provider, so two providers cannot collide.

    Keys used to be the model name alone; a model served through two providers
    made ``Tokens`` raise ``DuplicateKey`` as soon as both were in the window.
    """
    install(conn, "model", "shared", owner="p1")
    install(conn, "model", "shared", owner="p2")
    model_use(conn, "shared", "p1")
    model_use(conn, "shared", "p2", ts=1_700_000_000_001)
    rows = {(r["model"], r["provider"]) for r in db.q_model_tokens(conn)}
    assert rows == {("shared", "p1"), ("shared", "p2")}


def test_an_unfinished_tool_counts_in_run_and_the_canonical_name_counts_once(conn):
    """`streaming` is not a fourth spelling of "finished", and `bash`/`shell` is one tool.

    The Tools row is the only place a reader checks "calls = ok + fail + run" at a
    glance. Counting only `running` left the equation short by every streaming call,
    and counting raw spellings made the health line disagree with the table.
    """
    statuses = ("completed", "error", "running", "streaming")
    for i, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO tool_calls(call_id, tool_name, status, ts)"
            " VALUES(?,?,?,?)", (f"call_{i}", "shell", status, 1_700_000_000_000 + i))
    conn.execute(
        "INSERT INTO tool_calls(call_id, tool_name, status, ts)"
        " VALUES('call_old','bash','completed',1700000000000)")
    conn.commit()

    rows = db.q_tools(conn)
    assert [r["tool_name"] for r in rows] == ["shell"], rows
    row = rows[0]
    assert (row["calls"], row["completed"], row["errored"], row["running"]) == (5, 2, 1, 2)
    assert row["calls"] == row["completed"] + row["errored"] + row["running"]
    assert db.overview(conn)["distinct_tools"] == 1
