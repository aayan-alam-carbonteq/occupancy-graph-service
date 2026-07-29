"""Stage 1 of the hatch guard: the PRIMARY write control.

Not defence in depth. `default_transaction_read_only` is a session default that
raw `BEGIN READ WRITE` defeats -- proven in
tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary, where
the INSERT commits. That was acceptable while we controlled every call site; it
is not once the agent writes SQL. Nothing downstream of this module is a write
control.

WHY POSTGRES'S OWN PARSER, NOT A HAND-WRITTEN LEXER. The first draft of this
module lexed the SQL itself and applied three textual rules, on the theory that
a guard you can read end to end is a guard you can trust. That theory did not
survive an adversarial pass. Both Critical findings were the same failure: code
that looked careful but simply DISAGREED with PostgreSQL's real lexer.

  * A carriage return ends a `--` comment in Postgres (scan.l defines
    non_newline as [^\\n\\r]). The hand lexer scanned only for "\\n", so
    `SELECT 1 --x\\r; DROP TABLE t` was seen as ONE statement here while the
    server ran two.
  * Quoting a lowercase identifier is a NO-OP to Postgres, so
    `SELECT "pg_read_file"('/etc/passwd')` calls exactly that function. The
    hand lexer erased quoted identifiers and saw nothing to refuse.

Neither bug was a slip in the rules; both were the rules being applied to a
different language than the server speaks. A write control whose correctness
depends on independently re-deriving another parser's lexer is the wrong shape.

So this module now parses with `pglast`, which wraps libpg_query -- PostgreSQL's
own parser sources, not a reimplementation. There is no second lexer to keep in
sync, and the rules become STRUCTURAL facts about the parse tree rather than
guesses about text.

VERSION SKEW. pglast 8.4 exposes PostgreSQL 18.4's grammar; the fixture (and the
partner corpus) run 17. The skew fails safe in BOTH directions. Syntax that 18
accepts and 17 rejects is passed through and produces a harmless server-side
syntax error -- 17 never executes it. Syntax that 18 rejects because it was
removed becomes a ParseError here, which is a refusal: a false positive, never a
bypass. There is no direction in which the newer grammar lets a write through an
older server.

WHAT THIS MODULE DOES NOT COVER. The structural rules bound the SHAPE of the
statement, not its effects: a function call is shaped exactly like a read
whether or not it writes. `SELECT nextval('s')` is one SelectStmt with no other
statement node anywhere in its tree, yet it commits a write. So the blocked
function list below is genuinely load-bearing rather than defence in depth --
and a denylist of names can never be proven complete. It is written as FAMILY
PREFIXES for that reason, and the partner credential being a read-only guest
remains the backstop for anything a future Postgres names in a family not listed
here.

False positives are acceptable and observable -- the refusal names the exact
statement type, function or clause. A false negative is not.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from typing import Any

from pglast import ast as pg_ast
from pglast import parse_sql
from pglast.enums import LockClauseStrength
from pglast.parser import ParseError

# The only two node types ending in "Stmt" a read-only query may contain.
# RawStmt is the parser's per-statement envelope; SelectStmt is the query.
#
# THIS IS DELIBERATELY NOT A DENYLIST OF DML. Enumerating INSERT / UPDATE /
# DELETE / MERGE would need an edit every time PostgreSQL grows a statement
# type, and the day it grows one we do not know about is the day the hole opens.
# Refusing EVERY *Stmt node except these two is strictly more conservative and
# needs no maintenance. It is also what kills DML-in-CTE without this module
# having to model CTEs at all: an InsertStmt nested six levels deep inside a
# subquery inside a CTE is still an InsertStmt node in the tree.
_ALLOWED_STMT_NODES = frozenset({"RawStmt", "SelectStmt"})

# Read-only in name only: these reach the filesystem, open new connections that
# are not bound by our session settings, execute a nested query string, or hold
# a resource past the statement timeout.
#
# FAMILY PREFIXES, NOT EXACT NAMES. An exact-match set is the classic failure
# mode of a denylist: it pins today's catalogue and silently opens a hole the
# day Postgres adds a sibling. The original draft of this set listed
# `pg_advisory_lock` but not `pg_try_advisory_lock`, and `pg_ls_dir` but not
# `pg_ls_logdir` / `pg_ls_waldir` / `pg_ls_tmpdir` / `pg_ls_archive_statusdir`
# -- every one of those was reachable. Prefixes cover the whole family
# including versions not yet released. False positives here are cheap: a
# function named `pg_sleep_total` is refused by name, and the refusal says
# which name.
_BLOCKED_PREFIXES = (
    "pg_read_",           # pg_read_file, pg_read_binary_file, ...
    "pg_ls_",             # pg_ls_dir, pg_ls_logdir, pg_ls_waldir, pg_ls_tmpdir, ...
    "pg_stat_file",
    "lo_",                # lo_import, lo_export, lo_get, lo_put, lo_unlink, ...
    "dblink",             # dblink, dblink_exec, dblink_connect, dblink_send_query, ...
    "postgres_fdw",
    "pg_sleep",           # pg_sleep, pg_sleep_for, pg_sleep_until
    "pg_advisory",        # pg_advisory_lock, pg_advisory_xact_lock, ...
    "pg_try_advisory",    # pg_try_advisory_lock, pg_try_advisory_xact_lock, ...
    "query_to_xml",       # query_to_xml, query_to_xmlschema, query_to_xml_and_xmlschema
    "pg_terminate_",
    "pg_cancel_",
    "pg_signal_",
    "pg_reload_",
    "pg_rotate_",
    "pg_logical_emit",
    "pg_import_",
    "pg_promote",
    "pg_create_",         # pg_create_restore_point, pg_create_logical_replication_slot
    "pg_drop_",
    "pg_replication_",    # ..._origin_advance, ..._slot_advance
    "pg_backup_",
    "pg_start_backup",
    "pg_stop_backup",
    "pg_switch_",
    "pg_wal_",            # pg_wal_replay_pause / _resume
    "pg_xlog_",           # pre-10 spelling of the same
    "pg_stat_reset",
    "pg_stat_statements", # ..._reset writes shared state
    "pg_export_",         # pg_export_snapshot holds a resource
    "pg_truncate_",       # pg_truncate_visibility_map WRITES a relation fork
    "pg_prewarm",
    "heap_force_",        # pg_surgery: heap_force_kill / heap_force_freeze
    "txid_",              # txid_current ASSIGNS a real transaction id
    "pg_current_xact",    # the PG13+ spelling of the same
)

# Exact names that do not belong to a family above.
#
# nextval/setval are the sharpest entries here: they are ordinary-looking
# SELECT-able functions that COMMIT A WRITE to a sequence, and they sail past
# every structural rule -- `SELECT nextval('s')` is a single SelectStmt with no
# intoClause, no locking clause and no other statement node in its tree. They
# are the clearest evidence that the structural rules bound the statement's
# SHAPE, not its effects, and that this list is doing load-bearing work rather
# than decorating.
#
# pg_notify is the function spelling of the NOTIFY statement, which the
# statement-node rule already denies; denying only the statement form would
# have been security theatre.
_BLOCKED_FUNCTIONS = frozenset({
    "nextval", "setval",
    "pg_notify",
    "set_config",
    "pg_relation_filepath",
    "pg_file_settings",
    "pg_hba_file_rules",
    "pg_current_logfile",
    "pg_config",
})

# For a refusal message that names the SQL the agent actually wrote.
_LOCK_SPELLING = {
    LockClauseStrength.LCS_FORKEYSHARE: "FOR KEY SHARE",
    LockClauseStrength.LCS_FORSHARE: "FOR SHARE",
    LockClauseStrength.LCS_FORNOKEYUPDATE: "FOR NO KEY UPDATE",
    LockClauseStrength.LCS_FORUPDATE: "FOR UPDATE",
}


class SqlRefused(Exception):
    """A query refused BEFORE execution. `stage` is "parse" or "explain"."""

    def __init__(self, stage: str, reason: str, hint: str = "") -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.hint = hint


def _fold(name: str) -> str:
    """Fold a parsed identifier to the form the function rules match against.

    libpg_query has already applied Postgres's own identifier downcasing, which
    is why this is no longer doing the heavy lifting it did when it ran over raw
    text -- the Kelvin sign in `dblinK` arrives here already folded to `dblink`
    by the parser itself.

    It is still load-bearing for COMPATIBILITY characters, which Postgres does
    NOT fold: `ｐｇ_ｓｌｅｅｐ` (full-width) survives the parser verbatim. NFKC
    maps those onto their ASCII equivalents and casefold removes what case
    distinction remains. That is deliberately MORE aggressive than the server,
    which would likely reject the full-width name as an unknown function; the
    guard refuses it rather than reasoning about whether some encoding or locale
    might resolve it. Refusing costs a false positive on an identifier no honest
    query contains.
    """
    return unicodedata.normalize("NFKC", name).casefold()


def _walk(node: Any) -> Iterator[pg_ast.Node]:
    """Yield every AST node in the tree, depth first.

    Recurses through the concrete node class's own `__slots__`, which pglast
    generates from libpg_query's node definitions, so the traversal enumerates
    exactly the fields the parser produces rather than a list maintained here.
    The base class's `ancestors` slot is skipped: it is a back-reference the
    visitor machinery populates and following it would cycle.
    """
    if isinstance(node, pg_ast.Node):
        yield node
        for slot in type(node).__slots__:
            if slot == "ancestors":
                continue
            yield from _walk(getattr(node, slot, None))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _refuse_blocked_function(call: pg_ast.FuncCall) -> None:
    """Refuse a FuncCall whose name reaches the blocked list.

    The name is read from the PARSE TREE, not from the query text, so every
    spelling the parser normalises is handled for free and none of it is modelled
    here: `"pg_read_file"(...)` (quoting a lowercase name is a no-op),
    `pg_catalog.pg_read_file(...)` (schema qualification), and `PG_READ_FILE(...)`
    (unquoted downcasing) all arrive as the single string 'pg_read_file'.

    Every element of a qualified name is checked, not just the last. The
    qualifier is nearly always `pg_catalog`, which matches nothing here, so this
    costs nothing -- and it means a blocked name can never be smuggled into the
    schema position of a call that resolves through a search_path we do not
    control.
    """
    for part in call.funcname or ():
        if not isinstance(part, pg_ast.String):
            continue
        name = _fold(part.sval)
        if name in _BLOCKED_FUNCTIONS or name.startswith(_BLOCKED_PREFIXES):
            raise SqlRefused("parse", f"function {name} is not permitted")


def parse(query: str) -> str:
    """Refuse anything that is not exactly one read-only SELECT.

    Returns the exact original text of the statement, sliced out by the parser's
    own offsets so it carries no trailing semicolon, ready for stage 2. Raises
    SqlRefused(stage="parse") otherwise.
    """
    text = (query or "").strip()
    if not text:
        raise SqlRefused("parse", "empty query")

    # 1. It must PARSE. Anything malformed is refused, never guessed at -- and
    #    this one branch subsumes every unterminated-literal, unterminated-
    #    comment and unterminated-dollar-quote case the hand lexer had to
    #    implement (and mis-implement) itself. The parser's own message is
    #    passed through because it names and locates the offending token, which
    #    is exactly what the agent needs to fix its own SQL.
    try:
        statements = parse_sql(text)
    except ParseError as exc:
        raise SqlRefused("parse", f"not valid SQL: {exc}") from None

    # 2. Exactly one statement -- a real statement count from Postgres's
    #    grammar, not semicolon-counting on text we masked ourselves. A query
    #    that is only comments parses to zero statements and is as empty as "".
    if len(statements) != 1:
        raise SqlRefused(
            "parse",
            "only one statement is permitted; PostgreSQL parsed "
            f"{len(statements)}" + (" (the query is empty or only comments)"
                                    if not statements else ""),
        )

    raw = statements[0]
    top = raw.stmt

    # 3. That statement must be a SELECT. `CREATE TABLE x AS SELECT ...` is a
    #    CreateTableAsStmt, not a SelectStmt, so it is refused right here.
    if not isinstance(top, pg_ast.SelectStmt):
        raise SqlRefused(
            "parse",
            f"only a SELECT statement is permitted; PostgreSQL parsed this as "
            f"{type(top).__name__}",
        )

    # 4-6. One walk of the whole tree.
    for node in _walk(raw):
        kind = type(node).__name__

        # 4. No statement node other than the SELECT itself, ANYWHERE. This is
        #    what refuses DML-in-CTE, at any nesting depth, without modelling
        #    CTEs.
        if kind.endswith("Stmt") and kind not in _ALLOWED_STMT_NODES:
            raise SqlRefused(
                "parse", f"{kind} is not permitted in a read-only query"
            )

        # 5. THE TRAP. `SELECT * INTO copy_of_records FROM t` CREATES A TABLE.
        #    It parses as an ordinary SelectStmt and contains NO statement node
        #    other than itself, so rule 4 sails straight past it. The only thing
        #    that distinguishes it from a plain read is a non-None intoClause.
        #    Checked on every SelectStmt in the tree, not just the top one, so a
        #    nested or set-operation arm cannot carry one past this.
        if isinstance(node, pg_ast.SelectStmt) and node.intoClause is not None:
            raise SqlRefused(
                "parse",
                "SELECT ... INTO creates a table and is not permitted; "
                "use a plain SELECT",
            )

        # 6. No row locks. FOR SHARE / FOR KEY SHARE / FOR UPDATE /
        #    FOR NO KEY UPDATE take real locks that outlive the statement inside
        #    a transaction and block other writers, so they are not read-only in
        #    any useful sense.
        if isinstance(node, pg_ast.LockingClause):
            spelling = _LOCK_SPELLING.get(node.strength, "FOR UPDATE/SHARE")
            raise SqlRefused(
                "parse", f"row lock {spelling} is not permitted in a read-only query"
            )

        # 7. No blocked function, at any depth and in any spelling.
        if isinstance(node, pg_ast.FuncCall):
            _refuse_blocked_function(node)

    # Return the statement's OWN text, cut by the parser's offsets.
    #
    # stmt_location is where this statement starts and stmt_len is its length,
    # with 0 meaning "to the end of the string" for the final statement. Slicing
    # by those offsets is what guarantees stage 2 never receives a live ';': the
    # parser puts the statement's end BEFORE its terminator, so the semicolon and
    # anything trailing it are outside the slice by construction rather than by a
    # string-manipulation rule we have to get right.
    #
    # It also fixes a latent defect in the old cut, which keyed on
    # `text.endswith(";")`: `"SELECT 1; -- done"` does not end with ';' after
    # .strip(), so the semicolon survived into the returned string and stage 2's
    # subquery wrap was left to turn it into a syntax error. Nothing downstream
    # of this module is a write control, so stage 1 must not emit a chainable
    # string at all.
    start = raw.stmt_location or 0
    end = start + raw.stmt_len if raw.stmt_len else len(text)
    return text[start:end].strip()


def wrap_with_limit(query: str, *, cap: int) -> str:
    """Stage 2: bound the result set.

    The query is WRAPPED in a subquery rather than having its LIMIT rewritten.
    Rewriting requires deciding which LIMIT is the outer one and whether it is
    inside a CTE or a subquery; wrapping caps unconditionally. A supplied LIMIT
    larger than the cap is overridden by the outer one, and a smaller one still
    wins because Postgres pushes the outer limit down.

    Newlines around the body are load-bearing: a trailing `-- comment` would
    otherwise swallow the closing parenthesis. Verified against the fixture in
    test_a_trailing_comment_cannot_evade_the_row_cap_in_practice -- a `--` ends
    at the newline, so `) AS _hatch` survives on its own line.

    int(cap) is not cosmetic -- it is the only reason no caller-supplied text
    can reach a SQL position here. It rejects "50; DROP TABLE t" (ValueError)
    and None (TypeError) rather than interpolating them. It also accepts bool,
    because bool IS an int subclass in Python: cap=True yields LIMIT 1. That is
    a caller bug rather than a hazard -- it errs toward FEWER rows, and every
    coercion int() performs (float truncation, bool) rounds toward a smaller
    cap, never a larger one.
    """
    return f"SELECT * FROM (\n{query}\n) AS _hatch\nLIMIT {int(cap)}"
