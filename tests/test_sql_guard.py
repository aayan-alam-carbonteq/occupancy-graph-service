"""The parse guard. Adversarial by design.

Task 11's review proved BEGIN READ WRITE defeats default_transaction_read_only
and the write commits (tests/test_pool.py). Nothing downstream of this stage is
a write control, so every attack shape below must be refused HERE.
"""
from __future__ import annotations

import asyncpg
import pytest

from occupancy_graph.service.sql_guard import SqlRefused, parse


def refuse(query: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        parse(query)
    assert caught.value.stage == "parse"
    return caught.value


def test_a_plain_select_is_accepted():
    assert parse("SELECT record_id FROM records_legacy LIMIT 5") == (
        "SELECT record_id FROM records_legacy LIMIT 5"
    )


def test_a_with_cte_select_is_accepted():
    query = "WITH z AS (SELECT 1 AS n) SELECT n FROM z"
    assert parse(query) == query


def test_a_trailing_semicolon_is_stripped_not_refused():
    assert parse("SELECT 1;  ") == "SELECT 1"


def test_an_empty_query_is_refused():
    assert "empty" in refuse("   ").reason


def test_a_non_select_first_keyword_is_refused():
    assert "SELECT or WITH" in refuse("EXPLAIN SELECT 1").reason


def test_chained_statements_are_refused():
    assert "one statement" in refuse("SELECT 1; SELECT 2").reason
    refuse("SELECT 1; DROP TABLE records_legacy")


def test_a_semicolon_inside_a_string_literal_is_not_a_chain():
    query = "SELECT * FROM records_legacy WHERE employer = 'ACME; DROP TABLE t'"
    assert parse(query) == query


def test_a_semicolon_inside_a_quoted_identifier_is_not_a_chain():
    query = 'SELECT 1 AS ";"'
    assert parse(query) == query


def test_a_semicolon_inside_a_dollar_quoted_literal_is_not_a_chain():
    query = "SELECT $tag$a; DROP TABLE t$tag$ AS s"
    assert parse(query) == query


def test_a_line_comment_cannot_hide_a_second_statement():
    refuse("SELECT 1 --\n; DROP TABLE records_legacy")


def test_a_block_comment_cannot_hide_a_write():
    refuse("SELECT 1 /* x */ ; INSERT INTO t VALUES (1)")
    # A comment that merely LOOKS like a write is harmless and must not refuse.
    assert parse("SELECT 1 /* not an INSERT */") == "SELECT 1 /* not an INSERT */"


def test_nested_block_comments_are_handled():
    assert parse("SELECT 1 /* a /* b */ c */") == "SELECT 1 /* a /* b */ c */"
    assert "unterminated" in refuse("SELECT 1 /* a /* b */").reason


def test_insert_inside_a_cte_is_refused():
    assert "INSERT" in refuse(
        "WITH w AS (INSERT INTO records_legacy (record_id) VALUES (1) RETURNING record_id) "
        "SELECT * FROM w"
    ).reason


def test_update_inside_a_cte_is_refused():
    refuse("WITH w AS (UPDATE records_legacy SET zip = '0' RETURNING zip) SELECT * FROM w")


def test_delete_inside_a_cte_is_refused():
    refuse("WITH w AS (DELETE FROM records_legacy RETURNING record_id) SELECT * FROM w")


def test_begin_read_write_is_refused():
    assert "BEGIN" in refuse("BEGIN READ WRITE").reason
    refuse("SELECT 1; BEGIN READ WRITE; INSERT INTO t VALUES (1)")


def test_commit_and_rollback_are_refused():
    refuse("COMMIT")
    refuse("SELECT 1; ROLLBACK")


def test_set_is_refused():
    refuse("SET default_transaction_read_only = off")
    refuse("SELECT 1; SET statement_timeout = 0")


def test_copy_is_refused():
    refuse("COPY records_legacy TO '/tmp/x.csv'")


def test_do_block_is_refused():
    refuse("DO $$ BEGIN PERFORM 1; END $$")


def test_call_is_refused():
    refuse("CALL some_procedure()")


def test_grant_and_alter_are_refused():
    refuse("GRANT ALL ON records_legacy TO PUBLIC")
    refuse("ALTER TABLE records_legacy ADD COLUMN x int")


def test_select_into_is_refused():
    assert "INTO" in refuse("SELECT * INTO copy_of_records FROM records_legacy").reason


def test_an_unterminated_string_literal_is_refused():
    assert "unterminated" in refuse("SELECT 'abc").reason


def test_blocked_functions_are_refused():
    assert "pg_read_file" in refuse("SELECT pg_read_file('/etc/passwd')").reason
    refuse("SELECT dblink('host=evil', 'SELECT 1')")
    refuse("SELECT pg_sleep(3600)")
    refuse("SELECT query_to_xml('SELECT 1', true, true, '')")


# --- Check A: tests that reach rule 3 rather than passing via rule 2 ---------
#
# Several refusals above pass for the WRONG reason. `refuse("BEGIN READ WRITE")`
# asserts "BEGIN" in reason, but the head check (rule 2) fires first and yields
# "query must begin with SELECT or WITH, got 'BEGIN'" -- the substring matches
# only because rule 2 echoes the offending word. That is a coincidence, not
# coverage of the no-keyword-anywhere rule. The same is true of COMMIT,
# ROLLBACK, SET, COPY, DO, CALL, GRANT and ALTER as written above: every one of
# them puts the keyword FIRST. These pin the same keywords in a position only
# rule 3 can catch.


@pytest.mark.parametrize(
    "keyword, query",
    [
        ("BEGIN", "SELECT 1 FROM t WHERE BEGIN"),
        ("COMMIT", "SELECT COMMIT FROM t"),
        ("ROLLBACK", "SELECT 1 AS x FROM ROLLBACK"),
        ("SET", "SELECT 1 FROM t GROUP BY SET"),
        ("COPY", "SELECT COPY FROM t"),
        ("DO", "SELECT DO FROM t"),
        ("CALL", "SELECT CALL FROM t"),
        ("GRANT", "SELECT GRANT FROM t"),
        ("ALTER", "SELECT ALTER FROM t"),
        ("CREATE", "SELECT CREATE FROM t"),
        ("DROP", "SELECT DROP FROM t"),
        ("TRUNCATE", "SELECT TRUNCATE FROM t"),
        ("MERGE", "SELECT MERGE FROM t"),
        ("EXECUTE", "SELECT EXECUTE FROM t"),
        ("PREPARE", "SELECT PREPARE FROM t"),
        ("VACUUM", "SELECT VACUUM FROM t"),
        ("LOCK", "SELECT LOCK FROM t"),
        ("NOTIFY", "SELECT NOTIFY FROM t"),
    ],
)
def test_a_statement_keyword_is_refused_away_from_the_head(keyword, query):
    """Rule 3, genuinely: the head is a legitimate SELECT, so rule 2 passes and
    only the scan-everywhere rule can refuse these."""
    reason = refuse(query).reason
    assert keyword in reason
    # Rule 2's message would name the head keyword instead.
    assert "must begin with" not in reason


def test_the_head_rule_and_the_anywhere_rule_are_distinguishable():
    """Pins the distinction the parametrised test above relies on, so a future
    refactor that collapses the two messages cannot make it vacuous."""
    assert "must begin with" in refuse("BEGIN READ WRITE").reason
    assert "not permitted in a read-only query" in refuse("SELECT 1 FROM BEGIN").reason


# --- Check B: adversarial inputs --------------------------------------------


def test_a_quote_inside_a_block_comment_does_not_open_a_literal():
    """The single left-to-right pass exists for exactly this: sequential regex
    passes would let the comment's apostrophe swallow the rest of the query."""
    assert parse("SELECT 1 /* ' */") == "SELECT 1 /* ' */"
    refuse("SELECT 1 /* ' */ ; DROP TABLE t")


def test_a_comment_opener_inside_a_literal_does_not_open_a_comment():
    assert parse("SELECT '--' AS dashes") == "SELECT '--' AS dashes"
    assert parse("SELECT '/*' AS slashstar") == "SELECT '/*' AS slashstar"
    # ...and the literal must not hide a chained statement either.
    refuse("SELECT '--' ; DROP TABLE t")


def test_a_comment_opener_inside_a_dollar_quote_does_not_open_a_comment():
    assert parse("SELECT $$--$$ AS s") == "SELECT $$--$$ AS s"
    refuse("SELECT $$--$$ ; DROP TABLE t")


def test_an_escaped_quote_in_an_e_string_cannot_reopen_the_statement():
    r"""E'\'' is a complete literal containing one quote. A scanner that treated
    the backslash as ordinary would see the literal close early and the rest
    become code -- or worse, would see the trailing text as a literal and miss
    the chained DROP entirely."""
    refuse(r"SELECT E'\'' ; DROP TABLE t")
    assert parse(r"SELECT E'\'' AS q") == r"SELECT E'\'' AS q"


def test_nested_dissimilar_dollar_quote_tags_are_not_confused():
    """$a$ ... $b$ ... $b$ ... $a$ -- the inner tag is just text inside the
    outer literal, so the outer must close on ITS OWN tag, not the first one
    it meets."""
    query = "SELECT $a$ x $b$ y $b$ z $a$ AS s"
    assert parse(query) == query
    refuse("SELECT $a$ x $b$ y $b$ z $a$ ; DROP TABLE t")


def test_a_dollar_quote_cannot_hide_a_chain_behind_a_similar_tag():
    refuse("SELECT $a$ hi $a$ ; INSERT INTO t VALUES (1)")


def test_keyword_matching_is_case_insensitive():
    refuse("SELECT 1 FROM t WHERE iNsErT")
    refuse("select 1; drop table t")


def test_a_unicode_homoglyph_cannot_smuggle_a_blocked_function():
    """NFKC + casefold is deliberately MORE aggressive than Postgres's own
    identifier folding, which is a per-character downcase that would generally
    NOT resolve these to the ASCII function. That asymmetry is the point: the
    guard refuses the homoglyph rather than reasoning about whether some server
    encoding or locale might resolve it. Refusing costs a false positive on an
    identifier no honest query contains."""
    # Kelvin sign (U+212A) NFKC-normalises to "K" and casefolds to "k".
    refuse("SELECT dblinK('host=evil', 'SELECT 1')")
    # Full-width letters normalise to ASCII under NFKC.
    refuse("SELECT ｐｇ_ｓｌｅｅｐ(1)")
    # A full-width head is refused too -- rule 2 compares with .upper(), which
    # does not fold, so this never reaches the database to fail there.
    assert "must begin with" in refuse("ＳＥＬＥＣＴ 1").reason


def test_a_quoted_blocked_function_name_is_still_refused():
    """THE quoted-identifier bypass. Quoting a lowercase name is a no-op to
    Postgres -- "pg_read_file" resolves to exactly that function -- so replacing
    a quoted identifier wholesale with an inert placeholder would erase the one
    thing rule 3 exists to catch. Verified executable in
    test_a_quoted_function_name_really_does_execute."""
    assert "pg_read_file" in refuse("""SELECT "pg_read_file"('/etc/passwd')""").reason
    refuse('SELECT "pg_sleep"(3600)')
    refuse('SELECT "dblink"(\'host=evil\', \'SELECT 1\')')


def test_a_schema_qualified_blocked_function_is_still_refused():
    """pg_catalog.pg_read_file(...) -- the schema qualifier does not change the
    function, and the word scan sees the bare name either side of the dot."""
    refuse("SELECT pg_catalog.pg_read_file('/etc/passwd')")
    refuse('SELECT "pg_catalog"."pg_read_file"(\'/etc/passwd\')')
    refuse("SELECT pg_catalog.pg_sleep(3600)")


def test_row_locks_are_refused():
    """FOR UPDATE is caught incidentally by the UPDATE keyword, but FOR SHARE
    and FOR KEY SHARE contain no otherwise-denied word. They take real row locks
    that block writers, so they are not read-only in any useful sense."""
    refuse("SELECT 1 FROM t FOR UPDATE")
    refuse("SELECT 1 FROM t FOR SHARE")
    refuse("SELECT 1 FROM t FOR KEY SHARE")
    refuse("SELECT 1 FROM t FOR NO KEY UPDATE")


# --- Check C: the blocked list is a family rule, not an exact-match set ------


@pytest.mark.parametrize(
    "listed, sibling",
    [
        # The original exact-match draft listed the left column and MISSED the
        # right one. Each pair is one family.
        ("pg_advisory_lock", "pg_try_advisory_lock"),
        ("pg_advisory_lock", "pg_try_advisory_xact_lock"),
        ("pg_ls_dir", "pg_ls_logdir"),
        ("pg_ls_dir", "pg_ls_waldir"),
        ("pg_ls_dir", "pg_ls_tmpdir"),
        ("pg_ls_dir", "pg_ls_archive_statusdir"),
        ("pg_read_file", "pg_read_server_files"),
        ("lo_import", "lo_unlink"),
        ("lo_import", "lo_from_bytea"),
        ("dblink", "dblink_fetch"),
        ("pg_sleep", "pg_sleep_for"),
        ("query_to_xml", "query_to_xmlschema"),
    ],
)
def test_a_blocked_function_family_covers_unlisted_siblings(listed, sibling):
    """An exact-match denylist pins today's catalogue and silently opens a hole
    the day Postgres ships a sibling. Both members of each family must refuse."""
    refuse(f"SELECT {listed}(1)")
    refuse(f"SELECT {sibling}(1)")


# --- Check B: bypasses found by attacking the first draft, now closed --------


def test_a_carriage_return_ends_a_line_comment_just_like_a_newline():
    """CRITICAL, found by attack. Postgres ends a `--` comment at \n OR \r
    (scan.l defines non_newline as [^\n\r]). The first draft scanned only for
    \n, so everything after a CR was swallowed as comment here while the server
    executed it -- defeating the one-statement rule outright.

    Proven against the fixture in
    test_a_carriage_return_really_does_end_a_comment_in_postgres."""
    refuse("SELECT 1 --x\r; DROP TABLE records_legacy")
    refuse("SELECT 1--\r;INSERT INTO t VALUES (1)")
    # \r\n must behave the same way, not leave a stray statement behind.
    refuse("SELECT 1 --x\r\n; DROP TABLE records_legacy")
    # A CR-terminated comment followed by ordinary code is still ACCEPTED --
    # the fix ends the comment, it does not refuse every CR.
    assert parse("SELECT 1 --x\r+ 2") == "SELECT 1 --x\r+ 2"


def test_sequence_writes_disguised_as_selects_are_refused():
    """CRITICAL, found by attack. nextval/setval are shaped exactly like reads
    -- one statement, SELECT head, no statement keyword -- and commit a write to
    a sequence. They pass all three structural rules, so only the blocked
    function list can stop them. This is the test that proves that list is
    load-bearing rather than decorative."""
    assert "nextval" in refuse("SELECT nextval('some_seq')").reason
    refuse("SELECT setval('some_seq', 1)")
    refuse("WITH z AS (SELECT nextval('s') AS n) SELECT n FROM z")
    # currval and lastval only READ the session's value, so they stay allowed --
    # the list is not a blanket ban on sequence functions.
    assert parse("SELECT currval('some_seq')") == "SELECT currval('some_seq')"


def test_the_function_spelling_of_a_blocked_statement_is_also_refused():
    """Denying the NOTIFY keyword while allowing pg_notify() would have been
    security theatre -- the function does the same thing."""
    refuse("SELECT pg_notify('chan', 'msg')")


def test_transaction_id_assignment_is_refused():
    """txid_current() ASSIGNS a real transaction id rather than reporting one,
    which is a write to shared state on an ostensibly read-only connection."""
    refuse("SELECT txid_current()")
    refuse("SELECT pg_current_xact_id()")


def test_stat_and_logfile_disclosure_functions_are_refused():
    refuse("SELECT pg_stat_statements_reset()")
    refuse("SELECT pg_current_logfile()")
    refuse("SELECT pg_stat_reset()")


def test_surgery_and_wal_functions_are_refused():
    """pg_surgery's heap_force_kill writes to a heap directly, bypassing MVCC."""
    refuse("SELECT heap_force_kill('t'::regclass, ARRAY['(0,1)']::tid[])")
    refuse("SELECT pg_wal_replay_pause()")
    refuse("SELECT pg_export_snapshot()")


async def test_a_carriage_return_really_does_end_a_comment_in_postgres(fixture_pool):
    """The evidence behind the CR fix. If \r did NOT end the comment, `+ 2`
    would be commented out and this would return 1."""
    async with fixture_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 --x\r + 2") == 3
        assert await conn.fetchval("SELECT 1 --x\n + 2") == 3
        # ...and with no line terminator at all it really is a comment.
        assert await conn.fetchval("SELECT 1 --x + 2") == 1


async def test_a_quoted_function_name_really_does_execute(fixture_pool):
    """Proves the bypass the branch above closes is REAL, not theoretical.

    If quoting neutered a function name, this would be a syntax error or an
    unknown-function error. It is neither: Postgres strips the quotes and calls
    the function, which is exactly why the guard must see the name inside them.
    pg_sleep(0) is the harmless member of a family the guard blocks."""
    async with fixture_pool.acquire() as conn:
        # Quoting a lowercase name is a no-op: both of these resolve and run.
        await conn.execute('SELECT "pg_sleep"(0)')
        await conn.execute('SELECT "pg_catalog"."pg_sleep"(0)')
        # Negative control -- quoting does NOT make an arbitrary name
        # resolvable, so the two calls above really did reach the real
        # function rather than being parsed as something inert.
        with pytest.raises(asyncpg.exceptions.UndefinedFunctionError):
            await conn.execute('SELECT "pg_sleep_not_a_real_function"(0)')


# --- Check E: what parse() RETURNS is what gets executed ---------------------


def test_a_trailing_comment_after_the_semicolon_is_cut_with_it():
    """`"SELECT 1; -- done"` does not end with ';' after .strip(), so a cut
    keyed on `text.endswith(";")` left the semicolon in the returned string and
    leaned on stage 2's wrap to turn it into a syntax error. Nothing downstream
    of this module is a write control, so stage 1 must not emit a chainable
    string at all."""
    assert parse("SELECT 1; -- done") == "SELECT 1"
    assert parse("SELECT 1;  ") == "SELECT 1"
    assert parse("SELECT 1; /* done */") == "SELECT 1"
    assert parse("SELECT 1 -- c\n;") == "SELECT 1 -- c"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1; -- done",
        "SELECT 1; /* x */",
        "SELECT 'a;b' FROM t",
        'SELECT 1 AS ";"',
        "SELECT $tag$a; DROP TABLE t$tag$ AS s",
        "WITH z AS (SELECT 1 AS n) SELECT n FROM z;",
        "SELECT * FROM t --",
        "SELECT 1 /* c */ ;",
    ],
)
def test_what_parse_returns_never_contains_an_executable_semicolon(query):
    """The contract stage 2 is entitled to rely on. Re-masking parse()'s own
    output must leave no ';' behind: any that remain live inside a literal or a
    comment and cannot chain a statement."""
    from occupancy_graph.service.sql_guard import strip_literals

    assert ";" not in strip_literals(parse(query))


def test_masking_preserves_offsets():
    """The invariant the offset-based semicolon cut depends on. If masking ever
    changed length, the cut would land in the wrong place and could truncate a
    query mid-token instead of at its semicolon."""
    from occupancy_graph.service.sql_guard import strip_literals

    for query in [
        "SELECT 1 /* comment */ FROM t",
        "SELECT 'literal' FROM t",
        "SELECT $tag$body$tag$ FROM t",
        'SELECT "ident" FROM t',
        "SELECT 1 -- trailing",
        r"SELECT E'\'' FROM t",
    ]:
        assert len(strip_literals(query)) == len(query), query


def test_placeholder_text_never_reaches_the_returned_query():
    """The mask exists only for analysis. parse() returns the ORIGINAL text, so
    a query's literals and comments must survive it byte for byte."""
    query = "SELECT 'ACME; DROP' AS employer /* keep me */ FROM records_legacy"
    assert parse(query) == query


# --- Stage 2: the row cap. Wrapping rather than rewriting: a textual LIMIT
# --- rewrite has to understand the query, and the whole point of stage 1 is
# --- that we do not have to.

from occupancy_graph.service.sql_guard import wrap_with_limit  # noqa: E402


def test_a_query_without_a_limit_gets_one():
    assert wrap_with_limit("SELECT 1", cap=50) == (
        "SELECT * FROM (\nSELECT 1\n) AS _hatch\nLIMIT 50"
    )


def test_a_supplied_limit_is_capped_by_the_outer_one():
    wrapped = wrap_with_limit("SELECT 1 LIMIT 100000", cap=50)
    assert wrapped.endswith("LIMIT 50")
    assert "LIMIT 100000" in wrapped


def test_a_trailing_line_comment_cannot_eat_the_closing_paren():
    wrapped = wrap_with_limit("SELECT 1 -- note", cap=50)
    assert "\n) AS _hatch" in wrapped
    assert wrapped.splitlines()[-2] == ") AS _hatch"


def test_the_cap_is_coerced_to_an_int_so_no_text_reaches_the_sql():
    assert wrap_with_limit("SELECT 1", cap=True).endswith("LIMIT 1")
    with pytest.raises((ValueError, TypeError)):
        wrap_with_limit("SELECT 1", cap="50; DROP TABLE t")


def test_a_cte_survives_the_wrap():
    wrapped = wrap_with_limit("WITH z AS (SELECT 1 AS n) SELECT n FROM z", cap=10)
    assert wrapped.startswith("SELECT * FROM (\nWITH z AS")


async def test_the_wrapped_form_actually_runs(fixture_pool):
    wrapped = wrap_with_limit("SELECT record_id FROM public.records_legacy", cap=2)
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch(wrapped)
    assert len(rows) == 2


# --- Check D/F: the stage-1 -> stage-2 handoff, proven against the fixture ---


def test_the_stage_one_output_is_what_stage_two_wraps():
    """parse() feeds wrap_with_limit directly, so the handoff is pinned on the
    exact strings rather than on each stage in isolation. `"SELECT 1; -- done"`
    is the input that used to arrive at stage 2 with a live ';' still in it."""
    assert wrap_with_limit(parse("SELECT 1; -- done"), cap=2) == (
        "SELECT * FROM (\nSELECT 1\n) AS _hatch\nLIMIT 2"
    )
    assert wrap_with_limit(parse("SELECT * FROM t --"), cap=2) == (
        "SELECT * FROM (\nSELECT * FROM t --\n) AS _hatch\nLIMIT 2"
    )


async def test_a_trailing_comment_cannot_evade_the_row_cap_in_practice(fixture_pool):
    """The plan CLAIMS the newlines make a trailing comment safe. This confirms
    it against a real server rather than trusting it: records_legacy holds more
    rows than the cap, so a swallowed `) AS _hatch\\nLIMIT n` would show up as
    more rows coming back, not as an error."""
    async with fixture_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM public.records_legacy")
        assert total > 2, "fixture must hold more rows than the cap to prove anything"
        for query in (
            "SELECT * FROM public.records_legacy --",
            "SELECT * FROM public.records_legacy /* c */",
            "SELECT * FROM public.records_legacy --x\r",
            "SELECT * FROM public.records_legacy LIMIT 999",
        ):
            wrapped = wrap_with_limit(parse(query), cap=2)
            assert len(await conn.fetch(wrapped)) == 2, query


async def test_breaking_out_of_the_subquery_parenthesis_cannot_evade_the_cap(fixture_pool):
    """The attack the wrap design actually invites: close the paren early, then
    absorb the trailing `) AS _hatch LIMIT n`. Absorbing it needs a comment or
    literal that never closes -- which is exactly what stage 1 refuses. So the
    two stages interlock; neither would be sufficient alone."""
    for query in (
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 /*",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 '",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 $q$",
    ):
        assert "unterminated" in refuse(query).reason

    # These get past stage 1 (nothing is unterminated) but cannot execute: the
    # tail is a stray paren, not extra rows.
    async with fixture_pool.acquire() as conn:
        for query in (
            "SELECT * FROM public.records_legacy) AS a LIMIT 999 --",
            "SELECT * FROM public.records_legacy) AS a LIMIT 999 /* */",
        ):
            wrapped = wrap_with_limit(parse(query), cap=2)
            with pytest.raises(asyncpg.exceptions.PostgresSyntaxError):
                await conn.fetch(wrapped)


def test_every_cap_coercion_errs_toward_fewer_rows():
    """int() is the only reason no caller text reaches a SQL position, and every
    coercion it performs rounds the cap DOWN, never up."""
    assert wrap_with_limit("SELECT 1", cap=50.9).endswith("LIMIT 50")
    assert wrap_with_limit("SELECT 1", cap=True).endswith("LIMIT 1")
    assert wrap_with_limit("SELECT 1", cap=False).endswith("LIMIT 0")
    assert wrap_with_limit("SELECT 1", cap="50").endswith("LIMIT 50")
    for bad in ("50; DROP TABLE t", None, float("inf"), float("nan"), "1e3"):
        with pytest.raises((ValueError, TypeError, OverflowError)):
            wrap_with_limit("SELECT 1", cap=bad)


async def test_a_non_positive_cap_returns_no_rows_or_errors_but_never_all_of_them(
    fixture_pool,
):
    """A negative cap is a caller bug. It must not degrade to "unbounded"."""
    async with fixture_pool.acquire() as conn:
        assert await conn.fetch(
            wrap_with_limit("SELECT * FROM public.records_legacy", cap=0)
        ) == []
        with pytest.raises(asyncpg.PostgresError):
            await conn.fetch(
                wrap_with_limit("SELECT * FROM public.records_legacy", cap=-1)
            )
