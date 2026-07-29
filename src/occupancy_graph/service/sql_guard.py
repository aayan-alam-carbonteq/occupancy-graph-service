"""Stage 1 of the hatch guard: the PRIMARY write control.

Not defence in depth. `default_transaction_read_only` is a session default that
raw `BEGIN READ WRITE` defeats -- proven in
tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary, where
the INSERT commits. That was acceptable while we controlled every call site; it
is not once the agent writes SQL. Nothing downstream of this module is a write
control.

WHY A HAND-WRITTEN LEXER, NOT A SQL PARSER LIBRARY. The value of this guard is
that it can be reasoned about completely. It has three rules -- one statement, a
SELECT/WITH head, and no statement keyword ANYWHERE -- applied to text with all
comments and literals removed. The "anywhere" rule is strictly more conservative
than an AST walk (it also refuses `WHERE col = 'INSERT'` written without quotes,
which is not valid SQL anyway) and it covers DML-in-CTE without modelling CTEs.
Every failure mode of the lexer fails toward refusal: an unterminated literal is
refused, and text mistakenly treated as code hits the keyword rule.

False positives are acceptable and observable -- the refusal names the exact
keyword. A false negative is not.

WHAT THIS MODULE DOES NOT COVER. Rules 1-3 bound the SHAPE of the statement, not
its effects: a function call is shaped exactly like a read whether or not it
writes. `SELECT nextval('s')` is one statement, has a SELECT head, and contains
no statement keyword, yet it commits a write. So the blocked-function list below
is genuinely load-bearing rather than defence in depth -- and a denylist of names
can never be proven complete. It is written as FAMILY PREFIXES for that reason,
and the partner credential being a read-only guest remains the backstop for
anything a future Postgres names in a family not listed here.
"""
from __future__ import annotations

import re
import unicodedata

# Keywords refused ANYWHERE in the statement, not merely at the head. INSERT /
# UPDATE / DELETE / MERGE are the four statements Postgres permits inside a CTE
# and are the reason this rule is positional-independent; the rest cannot reach
# a CTE but cost nothing to deny and close off any chaining the ';' rule misses.
# INTO blocks `SELECT ... INTO newtable`. RETURNING cannot appear in a plain
# SELECT, so denying it is free and is a second signal on DML-in-CTE.
#
# SHARE and KEY are here for `SELECT ... FOR SHARE` / `FOR KEY SHARE`: those are
# row locks, they take real locks that outlive the statement inside a
# transaction and block other writers, and unlike `FOR UPDATE` / `FOR NO KEY
# UPDATE` they contain no otherwise-denied word. `OF` is not denied (it is a
# common column name and `FOR ... OF` is unreachable without SHARE or UPDATE).
_STATEMENT_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "INTO", "RETURNING",
    "CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "COPY", "DO", "CALL",
    "BEGIN", "COMMIT", "ROLLBACK", "START", "SAVEPOINT", "RELEASE",
    "SET", "RESET", "VACUUM", "ANALYZE", "CLUSTER", "REINDEX", "REFRESH",
    "LOCK", "PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD", "LISTEN",
    "UNLISTEN", "NOTIFY", "IMPORT", "EXPLAIN", "SHARE", "KEY",
})

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
# including versions not yet released. False positives here are cheap: a column
# named `pg_sleep_total` is refused by name, and the refusal says which name.
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
# all three structural rules -- `SELECT nextval('s')` is a single statement with
# a SELECT head and no statement keyword. They are the clearest evidence that
# rules 1-3 bound the statement's SHAPE, not its effects, and that this list is
# doing load-bearing work rather than decorating.
#
# pg_notify is the function spelling of the NOTIFY statement, which
# _STATEMENT_KEYWORDS already denies; denying only the keyword form would have
# been security theatre.
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

_WORD = re.compile(r"[^\W\d][\w$]*", re.UNICODE)
_DOLLAR_TAG = re.compile(r"\$([^\W\d]\w*)?\$", re.UNICODE)
_NON_WORD = re.compile(r"[^\w$]", re.UNICODE)
# Postgres ends a `--` comment at EITHER \n or \r (scan.l: non_newline [^\n\r]).
_LINE_COMMENT_END = re.compile(r"[\n\r]")


class SqlRefused(Exception):
    """A query refused BEFORE execution. `stage` is "parse" or "explain"."""

    def __init__(self, stage: str, reason: str, hint: str = "") -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.hint = hint


def _fold(word: str) -> str:
    """Fold a word to the form the keyword/function rules match against.

    Postgres downcases unquoted identifiers with a Unicode-aware fold, so
    `SELECТ` styled attacks and full-width or Kelvin-sign homoglyphs must
    not slip past an ASCII-only `.upper()`. NFKC maps the compatibility
    characters onto their ASCII equivalents; casefold then removes every case
    distinction, including the Kelvin sign K -> k and the dotless i.
    """
    return unicodedata.normalize("NFKC", word).casefold()


def strip_literals(sql: str) -> str:
    """Mask every comment, string literal, dollar-quoted body and quoted
    identifier, in ONE left-to-right pass, PRESERVING LENGTH AND OFFSETS.

    A single pass is required, not sequential regex passes: `/* \' */` must be
    recognised as a comment before the quote scanner sees it, and `\'-- \'` as a
    literal before the comment scanner does. Anything unterminated raises --
    ambiguity is refused, never guessed.

    The output is the same length as the input and every character keeps its
    index. That is what lets parse() find the exact offset of a trailing
    semicolon in the MASKED text and cut the ORIGINAL text there, so the string
    it returns provably contains no executable `;`. Masked spans become spaces,
    except inside a quoted identifier, where word characters are kept (see
    below) -- spaces are token separators, so blanking a literal can never fuse
    two neighbouring tokens into one.
    """
    out: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]
        start = index

        if sql.startswith("--", index):
            # A carriage return ends a line comment just like a newline does:
            # Postgres's scanner defines non_newline as [^\n\r]. Scanning only
            # for "\n" let `SELECT 1 --x\r; DROP TABLE t` past the one-statement
            # rule, because everything after the CR was swallowed as comment
            # here while the server executed it. Verified against the fixture:
            # `SELECT 1 --x\r + 2` returns 3, not 1.
            end = _LINE_COMMENT_END.search(sql, index)
            index = length if end is None else end.start()
            out.append(" " * (index - start))
            continue

        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise SqlRefused("parse", "unterminated block comment")
            out.append(" " * (index - start))
            continue

        # E\'...\' escape strings: a backslash escapes a quote, unlike a standard
        # literal under standard_conforming_strings.
        is_estring = (
            char in "Ee"
            and index + 1 < length
            and sql[index + 1] == "\'"
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_"))
        )
        if is_estring or char == "\'":
            index += 2 if is_estring else 1
            closed = False
            while index < length:
                if is_estring and sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                if sql[index] == "\'":
                    if index + 1 < length and sql[index + 1] == "\'":
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise SqlRefused("parse", "unterminated string literal")
            out.append(" " * (index - start))
            continue

        if char == '"':
            index += 1
            closed = False
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise SqlRefused("parse", "unterminated quoted identifier")
            # NOT blanked. A quoted identifier is the one construct that would be
            # erased from analysis while still executing as CODE: quoting a
            # lowercase name is a no-op to Postgres, so
            # `SELECT "pg_read_file"(\'/etc/passwd\')` resolves to exactly the
            # function of that name -- proven executable in
            # test_a_quoted_function_name_really_does_execute. Blanking it, or
            # replacing it with a placeholder as the first draft did, HIDES the
            # one thing rule 3 exists to catch.
            #
            # So word characters are kept and every other character -- `;`,
            # quotes, `/*`, `--`, parens -- becomes a space. That leaves the bare
            # NAME visible to rules 2 and 3 while neutralising anything
            # structural the identifier could smuggle. Keywords cannot be
            # smuggled this way in the first place (quoting strips a word\'s
            # keyword-ness, so `AS "insert"` is a legal alias); refusing those is
            # a false positive we accept to keep this a single rule.
            out.append(_NON_WORD.sub(" ", sql[start:index]))
            continue

        if char == "$":
            tag_match = _DOLLAR_TAG.match(sql, index)
            if tag_match:
                tag = tag_match.group(0)
                end_at = sql.find(tag, tag_match.end())
                if end_at == -1:
                    raise SqlRefused("parse", "unterminated dollar-quoted string")
                index = end_at + len(tag)
                out.append(" " * (index - start))
                continue

        out.append(char)
        index += 1

    masked = "".join(out)
    if len(masked) != len(sql):  # pragma: no cover -- invariant guard
        raise SqlRefused("parse", "internal: masking changed the query length")
    return masked


def parse(query: str) -> str:
    """Refuse anything that is not exactly one read-only SELECT.

    Returns the original query with a trailing semicolon removed, ready for
    stage 2. Raises SqlRefused(stage="parse") otherwise.
    """
    text = (query or "").strip()
    if not text:
        raise SqlRefused("parse", "empty query")

    # Same length as `text`, with every literal and comment blanked, so an
    # offset found here is the SAME offset in `text`.
    masked = strip_literals(text)

    # 1. Exactly one statement. A ';' inside a literal or comment is already
    #    blanked, so any ';' left other than a single trailing one is a chain.
    #
    #    The cut is by OFFSET, not by `text.endswith(";")`. Those differ
    #    whenever a comment trails the semicolon: `"SELECT 1; -- done"` does not
    #    end with ';' after .strip(), so the first draft returned it verbatim --
    #    handing stage 2 a string with a live ';' in it and relying on the
    #    subquery wrap to turn that into a syntax error. Since nothing
    #    downstream of this module is a write control, stage 1 must not emit a
    #    chainable string at all. Cutting at the masked offset drops the
    #    semicolon AND the trailing comment.
    trimmed = masked.rstrip()
    if trimmed.endswith(";"):
        cut = len(trimmed) - 1
        body = masked[:cut]
        text = text[:cut].rstrip()
    else:
        body = masked
    if ";" in body:
        raise SqlRefused(
            "parse", "only one statement is permitted; a ';' separator was found"
        )

    # 2. The statement must be a SELECT (a leading WITH is a CTE chain onto one).
    head = _WORD.search(body)
    keyword = head.group(0).upper() if head else ""
    if keyword not in {"SELECT", "WITH"}:
        raise SqlRefused(
            "parse", f"query must begin with SELECT or WITH, got {keyword or '<nothing>'!r}"
        )

    # 3. No statement keyword and no blocked function ANYWHERE.
    for match in _WORD.finditer(body):
        word = match.group(0)
        folded = _fold(word)
        if folded.upper() in _STATEMENT_KEYWORDS:
            raise SqlRefused(
                "parse",
                f"keyword {folded.upper()} is not permitted in a read-only query",
            )
        if folded in _BLOCKED_FUNCTIONS or folded.startswith(_BLOCKED_PREFIXES):
            raise SqlRefused("parse", f"function {folded} is not permitted")

    return text
