"""参考只读全树校验的收紧适配：表列授权、真实外键及确认条件不依赖Prompt。"""

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from contest_backend.errors import ContestError
from contest_backend.registry import ConditionSpec, SourceSpec
from contest_backend.schema_linking import LinkResult

# 解析器的诊断可能含SQL原文；该独立服务只发布自己的安全错误码。
logging.getLogger("sqlglot").disabled = True

SAFE_FUNCTIONS = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX", "ABS", "ROUND", "FLOOR", "CEIL", "CEILING",
    "LOWER", "UPPER", "LENGTH", "TRIM", "SUBSTRING", "CONCAT", "COALESCE", "NULLIF", "GREATEST", "LEAST",
    "CAST", "EXTRACT", "DATE_TRUNC", "TIMESTAMP_TRUNC", "CURRENT_DATE", "CURRENT_TIMESTAMP", "ROW_NUMBER", "RANK", "DENSE_RANK"})
SAFE_CAST_TYPES = frozenset({"BOOLEAN", "SMALLINT", "INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE", "TEXT",
                             "VARCHAR", "CHAR", "DATE", "TIME", "TIMETZ", "TIMESTAMP", "TIMESTAMPTZ", "UUID"})
FORBIDDEN = frozenset({"Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Into", "Copy", "Command",
    "Execute", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Set", "Use", "Lock", "TableSample",
    "LoadData", "TruncateTable", "Cache", "Uncache", "Hint", "Collate"})


def identifier(node) -> str:
    """遵循PG引号大小写规则，不把引号内大小写折叠成另一个授权对象。"""
    if node is None:
        return ""
    return node.name if isinstance(node, exp.Identifier) and node.args.get("quoted") else node.name.lower()


def parse_readonly(sql: str):
    """完整解析单句及所有子树；类型仅接受内置枚举允许集，未知节点失败关闭。"""
    try:
        if not sql.strip() or len(sql) > 20000:
            raise ValueError("语句超限")
        statements = [item for item in sqlglot.parse(sql, read="postgres") if item is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("非单句查询")
        tree = statements[0]
        nodes = list(tree.walk())
        if len(nodes) > 2048 or sum(isinstance(item, exp.Select) for item in nodes) > 8:
            raise ValueError("查询结构超限")
        for node in nodes:
            if (type(node).__name__ in FORBIDDEN or isinstance(node, exp.DataType)
                    and (not isinstance(node.this, exp.DataType.Type) or node.this.name not in SAFE_CAST_TYPES)):
                raise ValueError("非只读节点")
            if isinstance(node, exp.Column) and node.args.get("catalog"):
                raise ValueError("跨库列引用")
            if isinstance(node, (exp.And, exp.Or, exp.Exists, exp.Case)) or isinstance(node, exp.If) and isinstance(node.parent, exp.Case):
                continue
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
                if name not in SAFE_FUNCTIONS or isinstance(node.parent, exp.Dot):
                    raise ValueError("函数不在内置允许集")
        return tree
    except (ValueError, sqlglot.errors.SqlglotError, RecursionError):
        raise ContestError("SQL_REJECTED", 403) from None


def normalize_value(spec: ConditionSpec, value: str) -> str:
    """只规范明确类型，不猜单位/期间或执行表达式；规范值用于Prompt和AST双重核对。"""
    try:
        if not value.strip() or len(value) > 1000 or any(ord(char) < 32 for char in value):
            raise ValueError("条件无效")
        if spec.value_type == "integer":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,17})", value):
                raise ValueError("整数无效")
            return str(int(value))
        if spec.value_type == "decimal":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?", value):
                raise ValueError("小数无效")
            return format(Decimal(value), "f")
        if spec.value_type == "date":
            return date.fromisoformat(value).isoformat()
        return value
    except (ValueError, InvalidOperation):
        raise ContestError("RESULT_INVALID", 422) from None


def conjuncts(node):
    """只展开AND，OR、子查询或函数包装不充当已确认过滤条件的证明。"""
    if isinstance(node, exp.Paren):
        return conjuncts(node.this)
    if isinstance(node, exp.And):
        return conjuncts(node.this) + conjuncts(node.expression)
    return [node]


def column_ref(column, scope) -> tuple[str, str] | None:
    """将当前作用域已限定列反查真实基础表；CTE/外层关联不猜测血缘。"""
    if not isinstance(column, exp.Column):
        return None
    source = scope.selected_sources.get(column.table)
    if source is None or not isinstance(source[1], exp.Table):
        return None
    return identifier(source[1].this), identifier(column.this)


def verify_joins(tree, linked: LinkResult) -> None:
    """JOIN必须连接新增右表和已有左表的完整外键；复杂多表血缘和自连接失败关闭。"""
    scopes = list(traverse_scope(tree))
    if len(linked.tables) > 1 and (not isinstance(tree, exp.Select) or len(scopes) != 1):
        raise ContestError("SQL_REJECTED", 403)
    for scope in scopes:
        physical = [identifier(item.this) for _, item in scope.selected_sources.values() if isinstance(item, exp.Table)]
        if len(physical) != len(set(physical)):
            raise ContestError("SQL_REJECTED", 403)
        joins = scope.expression.args.get("joins") or []
        right_aliases = {join.this.alias_or_name for join in joins}
        connected = set(scope.selected_sources) - right_aliases
        for join in joins:
            if join.args.get("method") or join.args.get("using") or str(join.args.get("kind") or "").upper() == "CROSS":
                raise ContestError("SQL_REJECTED", 403)
            on = join.args.get("on")
            right = join.this.alias_or_name
            if on is None or not isinstance(join.this, exp.Table) or right in connected or not connected:
                raise ContestError("SQL_REJECTED", 403)
            pairs = set()
            for comparison in conjuncts(on):
                if not isinstance(comparison, exp.EQ):
                    raise ContestError("SQL_REJECTED", 403)
                a, b = column_ref(comparison.this, scope), column_ref(comparison.expression, scope)
                aliases = {comparison.this.table, comparison.expression.table} if a is not None and b is not None else set()
                if (a is None or b is None or a[0] == b[0] or right not in aliases
                        or len(aliases & connected) != 1):
                    raise ContestError("SQL_REJECTED", 403)
                pairs.add(frozenset((a, b)))
            known = [set(frozenset(((key.from_table, a), (key.to_table, b))) for a, b in key.pairs)
                     for key in linked.keys]
            if pairs not in known:
                raise ContestError("SQL_REJECTED", 403)
            connected.add(right)


def verify_conditions(tree, source: SourceSpec, linked: LinkResult, confirmed_slots: dict[str, str]) -> None:
    """机械核对已确认参数和选中列；无法证明复杂查询的条件覆盖时明确拒绝执行。"""
    filters = [item for item in source.conditions if item.slot in confirmed_slots]
    if not filters and not linked.selected_columns:
        return
    scopes = list(traverse_scope(tree))
    if not isinstance(tree, exp.Select) or len(scopes) != 1 or list(tree.find_all(exp.CTE)):
        raise ContestError("SQL_REJECTED", 403)
    scope = scopes[0]
    projected = {column_ref(column, scope) for expression in tree.expressions for column in expression.find_all(exp.Column)}
    if any(tuple(target.split(".", 1)) not in projected for target in linked.selected_columns):
        raise ContestError("SQL_REJECTED", 403)
    where = tree.args.get("where")
    predicates = conjuncts(where.this) if where is not None else []
    for spec in filters:
        expected = normalize_value(spec, confirmed_slots[spec.slot])
        kind = {"eq": exp.EQ, "gte": exp.GTE, "lte": exp.LTE}[spec.operator]
        found = False
        for predicate in predicates:
            if not isinstance(predicate, kind) or column_ref(predicate.this, scope) != (spec.table, spec.column):
                continue
            literal = predicate.expression
            if isinstance(literal, exp.Cast) and spec.value_type == "date":
                literal = literal.this
            if isinstance(literal, exp.Neg) and isinstance(literal.this, exp.Literal):
                actual = "-" + str(literal.this.this)
            elif isinstance(literal, exp.Literal):
                actual = str(literal.this)
            else:
                continue
            if normalize_value(spec, actual) == expected:
                found = True
                break
        if not found:
            raise ContestError("SQL_REJECTED", 403)


def prepare_sql(sql: str, source: SourceSpec, linked: LinkResult, confirmed_slots: dict[str, str]) -> str:
    """按当前授权表列限定AST并二次校验渲染结果，输出将实际执行的只读语句。"""
    tree = parse_readonly(sql)
    authorized = {name: columns for name, columns in source.table_map().items() if name in linked.tables}
    try:
        seen = set()
        if any(identifier(column.args.get("db")) not in {"", source.schema_name} for column in tree.find_all(exp.Column)):
            raise ValueError("跨Schema列引用")
        for scope in traverse_scope(tree):
            for _, item in scope.selected_sources.values():
                if not isinstance(item, exp.Table):
                    continue
                if (not isinstance(item.this, exp.Identifier) or item.args.get("catalog")
                        or identifier(item.args.get("db")) not in {"", source.schema_name}
                        or identifier(item.this) not in authorized):
                    raise ValueError("表不在授权范围")
                seen.add(identifier(item.this))
                item.set("db", exp.Identifier(this=source.schema_name, quoted=True))
                item.set("this", exp.Identifier(this=identifier(item.this), quoted=True))
        if seen != linked.tables:
            raise ValueError("查询没有使用已确认的真实表")
        # qualify按受限列目录展开星号并拒绝未知/歧义列，而非让数据库自行解析隐藏列。
        schema = MappingSchema({source.schema_name: authorized}, dialect="postgres", normalize=False)
        tree = qualify(tree, dialect="postgres", schema=schema,
                       infer_schema=False, validate_qualify_columns=True, quote_identifiers=True, identify=True)
        verify_joins(tree, linked)
        verify_conditions(tree, source, linked, confirmed_slots)
        rendered = tree.sql(dialect="postgres", unsupported_level=sqlglot.ErrorLevel.RAISE)
        parse_readonly(rendered)
        # 外层LIMIT控制实际读取上限；truncated仅由第501行真实存在确定。
        query = 'SELECT * FROM (' + rendered + ') AS "_contest_result" LIMIT 501'
        if len(query) > 20000:
            raise ValueError("实际SQL超过响应契约上限")
        return query
    except ContestError:
        raise
    except (ValueError, KeyError, sqlglot.errors.SqlglotError, RecursionError):
        raise ContestError("SQL_REJECTED", 403) from None
