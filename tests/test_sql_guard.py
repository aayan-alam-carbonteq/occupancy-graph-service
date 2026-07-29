"""The parse guard. Adversarial by design.

Task 11's review proved BEGIN READ WRITE defeats default_transaction_read_only
and the write commits (tests/test_pool.py). Nothing downstream of this stage is
a write control, so every attack shape below must be refused HERE.

This corpus outlived a complete rewrite of the module it guards. The first
implementation lexed SQL by hand; this one parses with pglast (libpg_query =
PostgreSQL's own parser). Every attack shape here kept its verdict across that
swap, which is the entire reason the swap was safe to make. Treat a verdict
change in this file as a security event, not a test-maintenance chore.
"""
from __future__ import annotations

import asyncpg
import pytest
from pglast import fingerprint, parse_sql

from occupancy_graph.service.sql_guard import SqlRefused, parse


def refuse(query: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        parse(query)
    assert caught.value.stage == "parse"
    return caught.value


# --- Rule 1: it must parse. Malformed SQL is refused, never guessed at. ------


def test_an_empty_query_is_refused():
    assert "empty" in refuse("   ").reason


def test_a_query_that_is_only_comments_is_as_empty_as_a_blank_one():
    """Zero statements is not "nothing to check", it is nothing to run."""
    assert "empty" in refuse("-- nothing here").reason
    assert "empty" in refuse("/* nothing */").reason
    assert "empty" in refuse(";;;").reason


def test_an_unterminated_string_literal_is_refused():
    assert "unterminated" in refuse("SELECT 'abc").reason


def test_every_unterminated_construct_is_refused_by_the_parser_itself():
    """The hand-written guard needed a branch per construct here, and got the
    line-comment one wrong. The parser supplies all of them, and its message
    names and locates the offending token."""
    assert "unterminated" in refuse("SELECT 1 /* a /* b */").reason
    assert "unterminated" in refuse("SELECT $q$ x").reason
    assert "unterminated" in refuse('SELECT "unclosed').reason
    assert "unterminated" in refuse(r"SELECT E'\'").reason


def test_syntactically_invalid_sql_is_refused_rather_than_passed_to_the_server():
    """The subquery-wrap breakout shapes. The hand-written guard ACCEPTED the
    last two -- nothing was unterminated and no keyword matched -- and leaned on
    the server to reject them. Refusing them here means stage 2 is never handed
    a string that only the server can adjudicate."""
    for query in (
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 /*",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 '",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 $q$",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 --",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 /* */",
    ):
        assert "not valid SQL" in refuse(query).reason, query


# --- Rule 2: exactly one statement, counted by PostgreSQL's grammar. --------


def test_chained_statements_are_refused():
    assert "one statement" in refuse("SELECT 1; SELECT 2").reason
    refuse("SELECT 1; DROP TABLE records_legacy")


def test_a_trailing_semicolon_is_stripped_not_refused():
    assert parse("SELECT 1;  ") == "SELECT 1"


def test_empty_statements_around_one_real_statement_are_not_a_chain():
    """`;;SELECT 1` is ONE statement to PostgreSQL -- an empty statement yields
    no parse node at all. The offset slice returns only the real statement, so
    the stray semicolons are dropped rather than forwarded to stage 2."""
    assert parse(";;SELECT 1") == "SELECT 1"
    assert parse("SELECT 1;;") == "SELECT 1"
    assert parse(";SELECT 1;") == "SELECT 1"
    # ...but a second REAL statement is still a chain.
    assert "one statement" in refuse("SELECT 1;;DROP TABLE t").reason


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


def test_a_carriage_return_ends_a_line_comment_just_like_a_newline():
    """CRITICAL, found by attacking the hand-written lexer. Postgres ends a `--`
    comment at \\n OR \\r (scan.l defines non_newline as [^\\n\\r]). The hand
    lexer scanned only for \\n, so everything after a CR was swallowed as comment
    while the server executed it -- defeating the one-statement rule outright.

    There is now no second lexer to get this wrong: the count comes from
    PostgreSQL's own grammar. Still pinned, because it is the bug that motivated
    the rewrite. Proven against the fixture in
    test_a_carriage_return_really_does_end_a_comment_in_postgres."""
    assert "one statement" in refuse("SELECT 1 --x\r; DROP TABLE records_legacy").reason
    refuse("SELECT 1--\r;INSERT INTO t VALUES (1)")
    refuse("SELECT 1 --x\r\n; DROP TABLE records_legacy")
    refuse("SELECT 1 --\r; SET default_transaction_read_only=off")
    # A CR-terminated comment followed by ordinary code is still ACCEPTED --
    # the comment ends, it does not make the query refusable.
    assert parse("SELECT 1 --x\r+ 2") == "SELECT 1 --x\r+ 2"


# --- Rule 3: the statement must BE a SelectStmt. ----------------------------


def test_a_non_select_statement_is_refused():
    assert "ExplainStmt" in refuse("EXPLAIN SELECT 1").reason


def test_the_statement_type_rule_fires_before_the_walk_and_has_its_own_message():
    """The statement-type rule and the tree walk BOTH refuse `COPY t TO '/x'` --
    the walk refuses any node whose type name ends in Stmt, and a top-level
    CopyStmt is one. So the parametrised test below cannot tell which rule fired,
    and deleting the statement-type check entirely would leave it green. (It did:
    that check was the one mutant this corpus initially failed to kill.)

    The distinction is load-bearing rather than cosmetic. The walk only catches
    the *Stmt NAMING CONVENTION; the statement-type rule catches anything that is
    not a SelectStmt, whatever PostgreSQL decides to call it. If a future
    top-level statement node ever falls outside that convention, this is the rule
    that still refuses it. These assertions make its removal visible."""
    reason = refuse("COPY records_legacy TO '/tmp/x.csv'").reason
    assert "only a SELECT statement is permitted" in reason
    assert "CopyStmt" in reason
    # The walk's message is genuinely different, so the two are not
    # interchangeable and neither assertion can be satisfied by the other rule.
    walk_reason = refuse(
        "WITH w AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM w"
    ).reason
    assert "InsertStmt is not permitted in a read-only query" in walk_reason
    assert "only a SELECT statement is permitted" not in walk_reason


@pytest.mark.parametrize(
    "node_type, query",
    [
        ("InsertStmt", "INSERT INTO t VALUES (1)"),
        ("UpdateStmt", "UPDATE t SET a = 1"),
        ("DeleteStmt", "DELETE FROM t"),
        ("TransactionStmt", "BEGIN READ WRITE"),
        ("TransactionStmt", "COMMIT"),
        ("TransactionStmt", "ROLLBACK"),
        ("VariableSetStmt", "SET default_transaction_read_only = off"),
        ("CopyStmt", "COPY records_legacy TO '/tmp/x.csv'"),
        ("CopyStmt", "COPY (SELECT 1) TO '/tmp/x'"),
        ("DoStmt", "DO $$ BEGIN PERFORM 1; END $$"),
        ("CallStmt", "CALL some_procedure()"),
        ("GrantStmt", "GRANT ALL ON records_legacy TO PUBLIC"),
        ("AlterTableStmt", "ALTER TABLE records_legacy ADD COLUMN x int"),
        ("DropStmt", "DROP TABLE records_legacy"),
        ("TruncateStmt", "TRUNCATE records_legacy"),
        ("VacuumStmt", "VACUUM records_legacy"),
        ("LockStmt", "LOCK TABLE records_legacy"),
        ("NotifyStmt", "NOTIFY chan"),
        ("PrepareStmt", "PREPARE p AS SELECT 1"),
        ("ExecuteStmt", "EXECUTE p"),
        ("ExplainStmt", "EXPLAIN ANALYZE SELECT 1"),
        ("DeclareCursorStmt", "DECLARE c CURSOR FOR SELECT 1"),
        ("RefreshMatViewStmt", "REFRESH MATERIALIZED VIEW mv"),
        ("CreateFunctionStmt",
         "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql"),
        # CREATE TABLE AS is the near-miss of the SELECT INTO trap: it also
        # creates a table from a query, but it is a DIFFERENT node type, so the
        # statement-type rule catches it and the intoClause rule never sees it.
        ("CreateTableAsStmt", "CREATE TABLE x AS SELECT 1"),
        ("CreateTableAsStmt", "CREATE MATERIALIZED VIEW mv AS SELECT 1"),
    ],
)
def test_a_statement_that_is_not_a_select_is_refused_by_node_type(node_type, query):
    """The refusal must NAME the node type. A later task turns `reason` into a
    message the agent reads; "refused" teaches it nothing, "CopyStmt is not
    permitted" teaches it exactly what to stop doing.

    The rule's own wording is asserted alongside the node type so that this stays
    a test of the STATEMENT-TYPE rule. Without it, the tree walk would refuse
    every one of these too and the check under test could be deleted unnoticed."""
    reason = refuse(query).reason
    assert node_type in reason
    assert "only a SELECT statement is permitted" in reason


def test_begin_read_write_is_refused():
    assert "TransactionStmt" in refuse("BEGIN READ WRITE").reason
    refuse("SELECT 1; BEGIN READ WRITE; INSERT INTO t VALUES (1)")


def test_commit_and_rollback_are_refused():
    refuse("COMMIT")
    refuse("SELECT 1; ROLLBACK")


def test_set_is_refused():
    refuse("SET default_transaction_read_only = off")
    refuse("SELECT 1; SET statement_timeout = 0")


# --- Rule 4: no statement node ANYWHERE in the tree except the SELECT. ------


def test_insert_inside_a_cte_is_refused():
    assert "InsertStmt" in refuse(
        "WITH w AS (INSERT INTO records_legacy (record_id) VALUES (1) RETURNING record_id) "
        "SELECT * FROM w"
    ).reason


def test_update_inside_a_cte_is_refused():
    refuse("WITH w AS (UPDATE records_legacy SET zip = '0' RETURNING zip) SELECT * FROM w")


def test_delete_inside_a_cte_is_refused():
    refuse("WITH w AS (DELETE FROM records_legacy RETURNING record_id) SELECT * FROM w")


@pytest.mark.parametrize(
    "node_type, query",
    [
        # A CTE inside a subquery inside a CTE -- the shape a rule that only
        # inspected the top-level WITH list would sail straight past.
        ("InsertStmt",
         "WITH a AS (SELECT * FROM (WITH b AS (INSERT INTO t VALUES (1) RETURNING id) "
         "SELECT * FROM b) q) SELECT * FROM a"),
        # Three levels of alternating CTE/subquery.
        ("UpdateStmt",
         "WITH a AS (SELECT * FROM (WITH b AS (SELECT * FROM (WITH c AS "
         "(UPDATE t SET x=1 RETURNING x) SELECT * FROM c) r) SELECT * FROM b) q) "
         "SELECT * FROM a"),
        ("DeleteStmt",
         "SELECT (SELECT * FROM (WITH d AS (DELETE FROM t RETURNING id) "
         "SELECT id FROM d) z)"),
        ("InsertStmt",
         "SELECT 1 WHERE EXISTS (WITH i AS (INSERT INTO t VALUES (1) RETURNING id) "
         "SELECT 1 FROM i)"),
        ("InsertStmt",
         "SELECT * FROM t, LATERAL (WITH i AS (INSERT INTO u VALUES (1) RETURNING id) "
         "SELECT * FROM i) l"),
        ("InsertStmt",
         "SELECT 1 UNION (WITH i AS (INSERT INTO t VALUES (1) RETURNING id) "
         "SELECT id FROM i)"),
        ("MergeStmt",
         "WITH m AS (MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN "
         "UPDATE SET x=1 RETURNING t.id) SELECT * FROM m"),
    ],
)
def test_dml_is_refused_at_any_nesting_depth(node_type, query):
    """Refusing EVERY node whose type name ends in Stmt -- rather than
    enumerating INSERT/UPDATE/DELETE/MERGE -- is what makes depth irrelevant and
    what means a statement type PostgreSQL has not shipped yet is already
    covered. The top-level statement here is a perfectly ordinary SelectStmt in
    every case; only the walk finds the DML."""
    assert node_type in refuse(query).reason


def test_the_top_level_select_is_not_itself_refused_by_the_walk():
    """The rule that refuses every *Stmt node must exempt the SELECT it is
    checking, and RawStmt, or it would refuse everything. This is the test that
    would fail if the allow-set were emptied."""
    assert parse("SELECT 1") == "SELECT 1"
    assert parse("SELECT (SELECT 1)") == "SELECT (SELECT 1)"


# --- Rule 5: THE TRAP. SELECT ... INTO creates a table. ---------------------


def test_select_into_is_refused():
    assert "INTO" in refuse("SELECT * INTO copy_of_records FROM records_legacy").reason


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 INTO x",
        "SELECT 1 INTO TABLE x",
        "SELECT 1 INTO TEMP x",
        "SELECT 1 INTO TEMPORARY x",
        "SELECT 1 INTO UNLOGGED x",
        "SELECT 1 INTO TEMP TABLE x",
        'SELECT 1 INTO "copy"',
        "SELECT 1 INTO public.x",
        "SELECT * INTO x FROM t",
        # Nested and set-operation positions: the intoClause is checked on EVERY
        # SelectStmt in the tree, not just the top one, so none of these can
        # carry one past the guard.
        "SELECT 1 INTO x UNION SELECT 2",
        "WITH z AS (SELECT 1) SELECT * INTO x FROM z",
        "WITH a AS (SELECT * INTO x FROM t) SELECT 1",
        "SELECT * FROM (SELECT 1 INTO x) q",
    ],
)
def test_select_into_creates_a_table_and_is_refused_in_every_spelling(query):
    """THE TRAP. `SELECT * INTO copy_of_records FROM t` CREATES A TABLE, yet it
    parses as a plain SelectStmt containing NO statement node other than itself.
    The statement-type rule and the walk BOTH accept it. Only a non-None
    intoClause distinguishes it from a read.

    The hand-written guard caught this with its INTO keyword rule; losing that
    coverage in the rewrite would have been a silent write hole."""
    assert "INTO" in refuse(query).reason


# --- Rule 6: no row locks. --------------------------------------------------


def test_row_locks_are_refused():
    """These take real row locks that block writers and outlive the statement
    inside a transaction, so they are not read-only in any useful sense. The
    hand-written guard caught them by denying the words SHARE and KEY; here they
    are a node type, so `FOR UPDATE OF t` and `FOR SHARE SKIP LOCKED` need no
    extra thought."""
    assert "FOR UPDATE" in refuse("SELECT 1 FROM t FOR UPDATE").reason
    assert "FOR SHARE" in refuse("SELECT 1 FROM t FOR SHARE").reason
    assert "FOR KEY SHARE" in refuse("SELECT 1 FROM t FOR KEY SHARE").reason
    assert "FOR NO KEY UPDATE" in refuse("SELECT 1 FROM t FOR NO KEY UPDATE").reason


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 FROM t FOR UPDATE NOWAIT",
        "SELECT 1 FROM t FOR UPDATE SKIP LOCKED",
        "SELECT 1 FROM t FOR UPDATE OF t",
        "SELECT * FROM (SELECT 1 FROM t FOR UPDATE) x",
        "WITH z AS (SELECT 1 FROM t FOR SHARE) SELECT * FROM z",
        "SELECT (SELECT 1 FROM t FOR SHARE)",
        "SELECT 1 UNION (SELECT 1 FROM t FOR SHARE)",
    ],
)
def test_a_row_lock_is_refused_wherever_it_appears(query):
    assert "row lock" in refuse(query).reason


# --- Rule 7: the blocked-function list. Load-bearing, not decorative. -------


def test_blocked_functions_are_refused():
    assert "pg_read_file" in refuse("SELECT pg_read_file('/etc/passwd')").reason
    refuse("SELECT dblink('host=evil', 'SELECT 1')")
    refuse("SELECT pg_sleep(3600)")
    refuse("SELECT query_to_xml('SELECT 1', true, true, '')")


def test_sequence_writes_disguised_as_selects_are_refused():
    """CRITICAL, found by attack. nextval/setval are shaped exactly like reads
    -- one SelectStmt, no intoClause, no locking clause, no other statement node
    anywhere in the tree -- and they COMMIT A WRITE to a sequence. They pass
    every structural rule, so only the blocked function list can stop them. This
    is the test that proves that list is load-bearing rather than decorative."""
    assert "nextval" in refuse("SELECT nextval('some_seq')").reason
    refuse("SELECT setval('some_seq', 1)")
    refuse("WITH z AS (SELECT nextval('s') AS n) SELECT n FROM z")
    # currval and lastval only READ the session's value, so they stay allowed --
    # the list is not a blanket ban on sequence functions.
    assert parse("SELECT currval('some_seq')") == "SELECT currval('some_seq')"
    assert parse("SELECT lastval()") == "SELECT lastval()"


def test_the_function_spelling_of_a_blocked_statement_is_also_refused():
    """Denying the NOTIFY statement while allowing pg_notify() would have been
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


@pytest.mark.parametrize(
    "spelling",
    [
        'SELECT "pg_sleep"(0)',                       # quoting a lowercase name
        "SELECT pg_catalog.pg_sleep(0)",              # schema qualification
        'SELECT "pg_catalog"."pg_sleep"(0)',          # both
        'SELECT "pg_catalog" . "pg_sleep" (0)',       # ...with whitespace
        "SELECT PG_SLEEP(0)",                         # unquoted upper-case
        "SELECT Pg_SlEeP(0)",                         # unquoted mixed case
        r'SELECT U&"pg_sle\0065p"(0)',                # unicode-escaped identifier
        "SELECT ｐｇ_ｓｌｅｅｐ(1)",                        # full-width homoglyphs
    ],
)
def test_a_blocked_function_is_refused_in_every_spelling_the_parser_normalises(spelling):
    """THE quoted-identifier bypass and its whole family. Quoting a lowercase
    name is a NO-OP to Postgres -- "pg_sleep" resolves to exactly that function
    -- which is why the hand-written guard's habit of erasing quoted identifiers
    hid the one thing its function rule existed to catch. Verified executable in
    test_a_quoted_function_name_really_does_execute.

    Reading the name from the PARSE TREE makes every one of these a single
    string, 'pg_sleep', with no spelling rule modelled here at all."""
    assert "pg_sleep" in refuse(spelling).reason


def test_a_quoted_blocked_function_name_is_still_refused():
    assert "pg_read_file" in refuse("""SELECT "pg_read_file"('/etc/passwd')""").reason
    refuse('SELECT "pg_sleep"(3600)')
    refuse('SELECT "dblink"(\'host=evil\', \'SELECT 1\')')
    # Quoting an UPPER-case name yields a name Postgres would NOT resolve to the
    # blocked function. Refusing it anyway is a false positive we accept.
    assert "nextval" in refuse("""SELECT "NEXTVAL"('s')""").reason


def test_a_schema_qualified_blocked_function_is_still_refused():
    refuse("SELECT pg_catalog.pg_read_file('/etc/passwd')")
    refuse('SELECT "pg_catalog"."pg_read_file"(\'/etc/passwd\')')
    refuse("SELECT pg_catalog.pg_sleep(3600)")
    # Every element of a qualified name is checked, not just the last, so a
    # blocked name cannot hide in the schema position either.
    refuse("SELECT public.nextval('s')")


def test_the_two_homoglyph_defences_are_different_mechanisms():
    """Worth separating, because only one of them is ours now.

    The Kelvin sign (U+212A) in `dblinK` is folded to 'k' by POSTGRES ITSELF
    during identifier downcasing -- the parse tree already says 'dblink' before
    this module looks at it. Full-width letters are NOT folded by Postgres, and
    survive into the tree verbatim; catching those is what _fold's NFKC pass is
    still for. A refactor that dropped _fold would keep the first line passing
    and break the second."""
    assert "dblink" in refuse("SELECT dblinK('host=evil', 'SELECT 1')").reason
    assert "pg_sleep" in refuse("SELECT ｐｇ_ｓｌｅｅｐ(1)").reason
    # A full-width SELECT is not valid SQL at all, so it never reaches a rule.
    assert "not valid SQL" in refuse("ＳＥＬＥＣＴ 1").reason


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM pg_ls_dir('/')",
        "SELECT * FROM ROWS FROM (pg_ls_dir('/'))",
        "SELECT * FROM t CROSS JOIN LATERAL pg_ls_dir('/')",
    ],
)
def test_a_blocked_function_in_the_from_clause_is_refused(query):
    """Set-returning functions are called from FROM, not from the target list.
    A rule that only inspected selected expressions would miss every one."""
    assert "pg_ls_dir" in refuse(query).reason


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM t WHERE id = nextval('s')",
        "SELECT (SELECT nextval('s'))",
        "SELECT max(nextval('s'))",
        "SELECT 1 FROM t ORDER BY nextval('s')",
        "SELECT 1 FROM t GROUP BY nextval('s')",
        "SELECT 1 FROM t GROUP BY 1 HAVING nextval('s') > 0",
        "SELECT CASE WHEN true THEN nextval('s') END",
        "SELECT count(*) OVER (ORDER BY nextval('s')) FROM t",
        "SELECT * FROM t, LATERAL (SELECT nextval('s')) x",
        "WITH z AS (SELECT nextval('s') AS n) SELECT n FROM z",
        "SELECT * FROM (VALUES (nextval('s'))) v",
        "SELECT 1 LIMIT (SELECT nextval('s'))",
        "SELECT nextval('s'::regclass)",
    ],
)
def test_a_blocked_function_is_refused_wherever_in_the_tree_it_hides(query):
    assert "nextval" in refuse(query).reason


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


# --- Literals and comments: the parser's job now, but still pinned. ---------
#
# These shapes broke the hand-written lexer's single-pass masking in review, and
# two of them were Critical. They are kept verbatim so that if this module ever
# regains a hand-rolled text pass, the same bugs cannot come back with it.


def test_a_quote_inside_a_block_comment_does_not_open_a_literal():
    assert parse("SELECT 1 /* ' */") == "SELECT 1 /* ' */"
    refuse("SELECT 1 /* ' */ ; DROP TABLE t")


def test_a_comment_opener_inside_a_literal_does_not_open_a_comment():
    assert parse("SELECT '--' AS dashes") == "SELECT '--' AS dashes"
    assert parse("SELECT '/*' AS slashstar") == "SELECT '/*' AS slashstar"
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
    refuse("select 1; drop table t")
    refuse("wItH w AS (iNsErT INTO t VALUES (1) RETURNING id) SELECT * FROM w")


# --- The one place the rewrite deliberately CHANGED a verdict. --------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT COPY FROM t",
        "SELECT MERGE FROM t",
        "SELECT 1 FROM t WHERE BEGIN",
        "SELECT 1 AS x FROM ROLLBACK",
        "SELECT 1 FROM t WHERE iNsErT",
        "SELECT LOCK FROM t",
        "SELECT NOTIFY FROM t",
    ],
)
def test_a_column_or_table_named_like_a_keyword_is_now_accepted(query):
    """DELIBERATE VERDICT CHANGE, recorded here rather than left implicit.

    The hand-written guard refused any statement keyword ANYWHERE in the text,
    and documented the resulting false positives as acceptable -- claiming they
    "would not be valid SQL anyway". That claim was wrong: most of these words
    are UNRESERVED in PostgreSQL, so `SELECT copy FROM t` is a legal read of a
    column named `copy`.

    Every query here parses to a SelectStmt containing nothing but column and
    table references. There is no statement node, no intoClause and no locking
    clause in any of them, which is exactly why the structural rules accept
    them. Nothing that can write became acceptable -- a false POSITIVE was
    removed, not a control. The real threat these tests were standing in for --
    DML away from the head of the statement -- is covered structurally by
    test_dml_is_refused_at_any_nesting_depth.
    """
    assert parse(query) == query
    stmt = parse_sql(query)[0].stmt
    assert type(stmt).__name__ == "SelectStmt"
    assert stmt.intoClause is None
    assert stmt.lockingClause is None


def test_reserved_keywords_in_that_position_are_still_a_syntax_error():
    """The counterpart: the words that are RESERVED cannot be identifiers, so
    these remain refused -- by the parser, not by a keyword list."""
    for query in ("SELECT DO FROM t", "SELECT GRANT FROM t", "SELECT CREATE FROM t"):
        assert "not valid SQL" in refuse(query).reason, query


# --- What parse() RETURNS is what gets executed. ----------------------------


def test_a_trailing_comment_after_the_semicolon_is_cut_with_it():
    """`"SELECT 1; -- done"` does not end with ';' after .strip(), so a cut
    keyed on `text.endswith(";")` left the semicolon in the returned string and
    leaned on stage 2's wrap to turn it into a syntax error. Nothing downstream
    of this module is a write control, so stage 1 must not emit a chainable
    string at all. The parser's stmt_location/stmt_len put the statement's end
    BEFORE its terminator, so this is now true by construction."""
    assert parse("SELECT 1; -- done") == "SELECT 1"
    assert parse("SELECT 1;  ") == "SELECT 1"
    assert parse("SELECT 1; /* done */") == "SELECT 1"
    assert parse("SELECT 1 -- c\n;") == "SELECT 1 -- c"


def test_leading_comments_and_whitespace_are_cut_by_the_offset_too():
    """stmt_location skips whatever precedes the statement, so the returned
    string starts at the statement itself."""
    assert parse("/* preamble */ SELECT 1") == "SELECT 1"
    assert parse("  -- note\n  SELECT 1  ") == "SELECT 1"


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
        ";;SELECT 1;;",
        "SELECT 1 --x\r+ 2",
    ],
)
def test_what_parse_returns_is_exactly_one_select_statement(query):
    """The contract stage 2 is entitled to rely on, checked by re-parsing rather
    than by re-masking. Any ';' still in the returned string must live inside a
    literal or a comment, and PostgreSQL is the authority on which those are:
    if one could chain a statement, this would report two."""
    returned = parse(query)
    statements = parse_sql(returned)
    assert len(statements) == 1, returned
    assert type(statements[0].stmt).__name__ == "SelectStmt"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'é'",
        "SELECT 'é';",
        "SELECT 'ééééé' FROM t;",
        "SELECT '😀' AS emoji;",
        "/* é */ SELECT 1;",
        "/* éééé */ SELECT 1",
        "SELECT 'café' FROM t -- é\n;",
        "SELECT '日本語テキスト' AS jp;",
        "SELECT 1 /* 😀😀 */ ;",
        'SELECT 1 AS "é";',
        "SELECT 'ACME; DROP' AS employer /* keep me */ FROM records_legacy",
    ],
)
def test_the_offset_slice_returns_the_same_query_it_was_given(query):
    """The invariant the offset cut depends on, and the one that would break
    silently if stmt_location/stmt_len were BYTE offsets while Python slices by
    CHARACTER -- a multibyte literal would then shift the cut and truncate the
    query mid-token, or drag a live ';' back into the returned string.

    pglast reports CHARACTER offsets; these pin that. fingerprint() compares the
    parsed structure rather than the text, so it proves the slice returned the
    same QUERY, not merely a similar-looking string."""
    returned = parse(query)
    assert fingerprint(returned) == fingerprint(query)


def test_comments_and_literals_survive_into_the_returned_query():
    """The guard analyses a parse tree but returns the ORIGINAL text, so a
    query's literals and comments must survive it byte for byte."""
    query = "SELECT 'ACME; DROP' AS employer /* keep me */ FROM records_legacy"
    assert parse(query) == query


# --- Fixture-backed evidence for the two Critical findings. -----------------


async def test_a_carriage_return_really_does_end_a_comment_in_postgres(fixture_pool):
    """The evidence behind the CR finding. If \r did NOT end the comment, `+ 2`
    would be commented out and this would return 1."""
    async with fixture_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 --x\r + 2") == 3
        assert await conn.fetchval("SELECT 1 --x\n + 2") == 3
        # ...and with no line terminator at all it really is a comment.
        assert await conn.fetchval("SELECT 1 --x + 2") == 1


async def test_a_quoted_function_name_really_does_execute(fixture_pool):
    """Proves the bypass the function rule closes is REAL, not theoretical.

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


async def test_select_into_really_does_create_a_table(fixture_pool):
    """The evidence behind THE TRAP. `SELECT * INTO x FROM t` parses as an
    ordinary SelectStmt with no other statement node in its tree, so if it did
    NOT actually create a relation the intoClause rule would be dead weight. It
    does.

    The transaction is opened with BEGIN READ WRITE deliberately, rather than
    relying on whatever state the pooled connection happens to be in. That is
    the same manoeuvre tests/test_pool.py uses to prove
    default_transaction_read_only is a session default rather than a boundary --
    so this test also demonstrates, on the same connection, exactly why stage 1
    has to be the write control. The rollback leaves no residue in the
    session-scoped fixture."""
    async with fixture_pool.acquire() as conn:
        await conn.execute("BEGIN READ WRITE")
        try:
            await conn.execute(
                "SELECT record_id INTO hatch_trap_table FROM public.records_legacy"
            )
            created = await conn.fetchval(
                "SELECT to_regclass('public.hatch_trap_table') IS NOT NULL"
            )
        finally:
            await conn.execute("ROLLBACK")
        assert created, "SELECT ... INTO must really create a relation"
        assert await conn.fetchval("SELECT to_regclass('public.hatch_trap_table')") is None


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


# --- The stage-1 -> stage-2 handoff, proven against the fixture. ------------


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


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1; -- done",
        "SELECT * FROM t --",
        "SELECT * FROM t --x\r",
        "SELECT 1 /* c */ ;",
        "WITH z AS (SELECT 1 AS n) SELECT n FROM z;",
        "SELECT 'é' AS accented;",
    ],
)
def test_the_wrapped_output_is_still_exactly_one_capped_select(query):
    """Structural half of the handoff proof: whatever stage 1 emits, the wrapped
    form must parse as ONE SelectStmt that still carries the outer LIMIT. A
    trailing comment that swallowed `) AS _hatch\\nLIMIT n` would show up here as
    a ParseError or as a missing limitCount."""
    statements = parse_sql(wrap_with_limit(parse(query), cap=2))
    assert len(statements) == 1
    assert statements[0].stmt.limitCount is not None


async def test_a_trailing_comment_cannot_evade_the_row_cap_in_practice(fixture_pool):
    """Empirical half of the handoff proof. The design CLAIMS the newlines make
    a trailing comment safe; this confirms it against a real server rather than
    trusting it. records_legacy holds more rows than the cap, so a swallowed
    `) AS _hatch\\nLIMIT n` would show up as more rows coming back, not as an
    error."""
    async with fixture_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM public.records_legacy")
        assert total > 2, "fixture must hold more rows than the cap to prove anything"
        for query in (
            "SELECT * FROM public.records_legacy",
            "SELECT * FROM public.records_legacy;",
            "SELECT * FROM public.records_legacy --",
            "SELECT * FROM public.records_legacy /* c */",
            "SELECT * FROM public.records_legacy --x\r",
            "SELECT * FROM public.records_legacy LIMIT 999",
            "SELECT * FROM public.records_legacy; -- done",
            "SELECT * FROM public.records_legacy -- é\n;",
            "/* preamble */ SELECT * FROM public.records_legacy --",
            "WITH z AS (SELECT * FROM public.records_legacy) SELECT * FROM z; -- x",
        ):
            wrapped = wrap_with_limit(parse(query), cap=2)
            assert len(await conn.fetch(wrapped)) == 2, query


async def test_breaking_out_of_the_subquery_parenthesis_is_refused_before_execution(
    fixture_pool,
):
    """The attack the wrap design actually invites: close the paren early, then
    absorb the trailing `) AS _hatch LIMIT n`.

    The hand-written guard ACCEPTED the last two of these -- nothing was
    unterminated and no keyword matched -- and relied on the server to reject
    them. Every one is now refused at stage 1, because an unbalanced paren is a
    syntax error to PostgreSQL's own parser. That is a TIGHTENING: the string
    never reaches the database at all."""
    for query in (
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 /*",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 '",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 $q$",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 --",
        "SELECT * FROM public.records_legacy) AS a LIMIT 999 /* */",
    ):
        assert "not valid SQL" in refuse(query).reason, query

    # ...and the server agrees they are not runnable, so nothing was lost by
    # refusing them here instead of there.
    async with fixture_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.PostgresSyntaxError):
            await conn.fetch(
                wrap_with_limit(
                    "SELECT * FROM public.records_legacy) AS a LIMIT 999 --", cap=2
                )
            )
