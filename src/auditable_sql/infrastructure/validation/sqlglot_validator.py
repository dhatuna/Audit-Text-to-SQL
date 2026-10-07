"""Static SQL validation on the parsed AST (sqlglot).

This is one of three layers. Even if something slips past here, the database role is
read-only with column-level grants and a statement timeout. The validator's job is to
reject early, with a precise reason that can be logged and fed back to the model.

Rule IDs are stable so audit records and eval reports can be aggregated by rule.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from auditable_sql.domain.models import ValidationResult, Violation
from auditable_sql.domain.policy import SqlPolicy

# Node types that must never appear anywhere in the tree, including inside CTEs/subqueries.
_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Command, exp.Set, exp.Copy, exp.Grant,
    exp.Into,   # SELECT ... INTO creates a table
    exp.Lock,   # SELECT ... FOR UPDATE takes row locks
    exp.Transaction, exp.Commit, exp.Rollback,
)  # fmt: skip

_DIALECT = "postgres"


def _v(rule: str, msg: str) -> Violation:
    return Violation(rule, msg)


class SqlglotValidator:
    def __init__(self, policy: SqlPolicy) -> None:
        self._policy = policy

    def validate(self, sql: str) -> ValidationResult:
        try:
            parsed = [s for s in sqlglot.parse(sql, read=_DIALECT) if s is not None]
        except SqlglotError as exc:
            return self._reject(_v("R001_PARSE_ERROR", str(exc).splitlines()[0][:300]))

        if len(parsed) != 1:
            return self._reject(
                _v("R002_STATEMENT_COUNT", f"expected exactly 1 statement, got {len(parsed)}")
            )
        stmt = parsed[0]

        if not isinstance(stmt, exp.Query):
            return self._reject(
                _v("R003_NOT_SELECT", f"only SELECT queries are allowed, got {stmt.key.upper()}")
            )

        violations: list[Violation] = []
        violations += self._forbidden_nodes(stmt)
        violations += self._functions(stmt)
        violations += self._tables(stmt)
        if violations:
            return self._reject(*violations)

        violations += self._columns(stmt)
        if violations:
            return self._reject(*violations)

        return ValidationResult(violations=(), normalized_sql=self._enforce_limit(stmt))

    # -- rules -----------------------------------------------------------------------

    def _forbidden_nodes(self, stmt: exp.Expr) -> list[Violation]:
        found = sorted({type(n).__name__ for n in stmt.walk() if isinstance(n, _FORBIDDEN_NODES)})
        return [_v("R004_FORBIDDEN_CLAUSE", f"{name} is not allowed") for name in found]

    def _functions(self, stmt: exp.Expr) -> list[Violation]:
        denied: set[str] = set()
        for fn in stmt.find_all(exp.Func):
            names = {fn.sql_name().lower(), type(fn).__name__.lower()}
            if isinstance(fn, exp.Anonymous):
                names.add(str(fn.name).lower())
            names |= {n.split(".")[-1] for n in names}
            denied |= {n for n in names if self._policy.is_function_denied(n)}
        # A user-defined schema-qualified call like pg_catalog.pg_sleep(1) parses as Dot(...).
        for dot in stmt.find_all(exp.Dot):
            fn = dot.expression
            if isinstance(fn, exp.Func):
                name = str(fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
                if self._policy.is_function_denied(name):
                    denied.add(name)
        return [
            _v("R008_FUNCTION_DENIED", f"function {n}() is not allowed") for n in sorted(denied)
        ]

    def _tables(self, stmt: exp.Expr) -> list[Violation]:
        cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
        out: list[Violation] = []
        for table in stmt.find_all(exp.Table):
            name = table.name.lower()
            schema = table.db.lower() if table.db else ""
            alias = table.args.get("alias")
            if not name:
                out.append(
                    _v("R010_TABLE_FUNCTION", f"table functions are not allowed: {table.sql()}")
                )
            elif table.catalog:
                out.append(_v("R005_TABLE_NOT_ALLOWED", f"cross-database reference {table.sql()}"))
            elif schema and (
                schema in self._policy.denied_schemas or schema != self._policy.default_schema
            ):
                out.append(_v("R005_TABLE_NOT_ALLOWED", f"schema '{schema}' is not readable"))
            elif not schema and name in cte_names:
                continue
            elif not self._policy.is_table_allowed(name):
                out.append(_v("R005_TABLE_NOT_ALLOWED", f"table '{name}' is not readable"))
            elif isinstance(alias, exp.TableAlias) and alias.columns:
                # FROM clients AS c(a, b) renames columns by position, which would let a
                # hidden column be read under an allowed name.
                out.append(
                    _v("R011_COLUMN_ALIAS_LIST", f"column alias lists on '{name}' are not allowed")
                )
        return _dedupe(out)

    def _columns(self, stmt: exp.Expr) -> list[Violation]:
        """Resolve every column reference to its source and check it against the policy.

        Deliberately does not rely on sqlglot's qualify(): it is built to make queries
        runnable, not to be a security boundary, and it mis-resolves some LATERAL cases.
        Anything this resolver cannot attribute to an allowed column is rejected.
        """
        out: list[Violation] = []
        for scope in traverse_scope(stmt):
            out += self._stars(scope)
            if not isinstance(scope.expression, exp.Select):
                continue  # LATERAL/UNION wrappers: their columns are checked in the inner SELECTs
            for col in scope.columns:
                if col.find_ancestor(exp.Select) is not scope.expression:
                    continue  # belongs to a nested scope, which is checked on its own
                problem = self._check_column(scope, col)
                if problem:
                    out.append(problem)
        return _dedupe(out)

    def _check_column(self, scope: Scope, col: exp.Column) -> Violation | None:
        name = col.name.lower()
        qualifier = col.table.lower() if col.table else ""

        if qualifier:
            source = _lookup(scope, qualifier)
            if source is None:
                return _v("R006_COLUMN_NOT_ALLOWED", f"cannot resolve '{col.sql()}'")
            if isinstance(source, exp.Table):
                if self._policy.is_column_allowed(source.name, name):
                    return None
                return _v(
                    "R006_COLUMN_NOT_ALLOWED", f"column '{source.name}.{name}' is not readable"
                )
            return (
                None
                if name in _derived_outputs(source)
                else _v(
                    "R006_COLUMN_NOT_ALLOWED", f"'{col.sql()}' is not an output of '{qualifier}'"
                )
            )

        # Unqualified: it must be an allowed column of a real table, or an output of a
        # derived table, somewhere in this scope or an enclosing one (correlation).
        walk: Scope | None = scope
        while walk is not None:
            for alias, source in walk.sources.items():
                if isinstance(source, Scope) and _encloses(source, scope):
                    continue  # a CTE/subquery cannot read its own output
                if isinstance(source, exp.Table):
                    if self._policy.is_column_allowed(source.name, name):
                        return None
                    if alias.lower() == name or source.name.lower() == name:
                        return _v(
                            "R009_WHOLE_ROW_REFERENCE",
                            f"'{name}' refers to an entire row of '{source.name}'",
                        )
                elif name in _derived_outputs(source):
                    return None
            walk = walk.parent
        return _v("R006_COLUMN_NOT_ALLOWED", f"column '{name}' is not readable")

    def _stars(self, scope: Scope) -> list[Violation]:
        select = scope.expression
        if not isinstance(select, exp.Select):
            return []
        out: list[Violation] = []
        for proj in select.expressions:
            if isinstance(proj, exp.Star):
                tables = [s.name for s in scope.sources.values() if isinstance(s, exp.Table)]
                if tables:
                    out.append(
                        _v("R007_SELECT_STAR", f"list columns explicitly instead of * on {tables}")
                    )
            elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                source = scope.sources.get(proj.table)
                if isinstance(source, exp.Table):
                    out.append(
                        _v("R007_SELECT_STAR", f"list columns explicitly instead of {proj.sql()}")
                    )
        return out

    # -- normalization -----------------------------------------------------------------

    def _enforce_limit(self, stmt: exp.Query) -> str:
        """Cap the row count. The cap is max_rows + 1 so the executor can detect truncation."""
        cap = self._policy.max_rows + 1
        limit = stmt.args.get("limit")
        current: int | None = None
        literal = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(literal, exp.Literal) and literal.is_int:
            current = int(literal.this)
        if current is None or current > cap:
            stmt = stmt.limit(cap, copy=True)  # replaces LIMIT/FETCH, including non-literal ones
        return stmt.sql(dialect=_DIALECT)

    @staticmethod
    def _reject(*violations: Violation) -> ValidationResult:
        return ValidationResult(violations=tuple(violations), normalized_sql=None)


def _lookup(scope: Scope, alias: str) -> exp.Table | Scope | None:
    walk: Scope | None = scope
    while walk is not None:
        for name, source in walk.sources.items():
            if name.lower() != alias:
                continue
            if isinstance(source, Scope) and _encloses(source, scope):
                continue  # a CTE/subquery cannot read its own output
            if isinstance(source, exp.Table | Scope):
                return source
        walk = walk.parent
    return None


def _encloses(source: Scope, scope: Scope) -> bool:
    node: exp.Expr | None = scope.expression
    while node is not None:
        if node is source.expression:
            return True
        node = node.parent
    return False


def _derived_outputs(source: Scope) -> set[str]:
    """Column names a subquery/CTE exposes, including an explicit alias list."""
    query = source.expression
    names = {n.lower() for n in query.named_selects} if isinstance(query, exp.Query) else set()
    wrapper = source.expression.parent
    if isinstance(wrapper, exp.CTE | exp.Subquery):
        alias = wrapper.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            names |= {c.name.lower() for c in alias.columns}
    return names


def _dedupe(violations: list[Violation]) -> list[Violation]:
    return list(dict.fromkeys(violations))
