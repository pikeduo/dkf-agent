"""只读全树、表列、外键及确认条件校验；不运行SQL或模型。"""

import pytest
from sqlglot import exp

from contest_backend.errors import ContestError
from contest_backend.registry import ConditionSpec
from contest_backend.schema_linking import LinkResult, SchemaLinker, tokens
from contest_backend.sql_policy import parse_readonly, prepare_sql


@pytest.mark.parametrize("query", [
    "UPDATE sales SET amount=0", "SELECT * FROM sales; DELETE FROM sales",
    "WITH changed AS (DELETE FROM sales RETURNING *) SELECT * FROM changed",
    "SELECT * INTO copied FROM sales", "SELECT * FROM sales FOR UPDATE",
    "SELECT secret FROM sales", "SELECT * FROM private.sales", "SELECT * FROM other.business.sales",
    "SELECT * FROM pg_catalog.pg_roles", "SELECT pg_sleep(1) FROM sales",
    "SELECT pg_read_file('/private') FROM sales", "SELECT custom_function(amount) FROM sales",
    "SELECT 'sales'::regclass FROM sales", "SELECT CAST('sales' AS regclass) FROM sales",
    "SELECT 'text'::regtype FROM sales", "SELECT amount::private.numeric FROM sales",
    "COPY sales TO STDOUT",
    "SELECT * FROM sales s JOIN sales x ON s.id=x.id",
])
def test_unsafe_or_unscoped_sql_is_rejected(source, query):
    """写入、多句、隐藏写CTE、危险函数、锁、系统或越权对象一律拒绝。"""
    with pytest.raises(ContestError) as caught:
        prepare_sql(query, source, LinkResult(tables={"sales"}), {})
    assert caught.value.code == "SQL_REJECTED"


@pytest.mark.parametrize("type_value", ["regclass", "INT", None, exp.Identifier(this="INT"), exp.DataType.Type.USERDEFINED])
def test_unknown_type_node_shapes_fail_closed(monkeypatch, type_value):
    """不同未知类型节点统一拒绝；字符串即便与允许类型同名也不获得内置类型权限。"""
    tree = exp.Select(expressions=[exp.Cast(this=exp.Literal.string("sales"), to=exp.DataType(this=type_value))])

    def parsed(query, **options):
        """只替代解析结果以覆盖节点表示差异，不连接数据库或模型。"""
        return [tree]

    monkeypatch.setattr("contest_backend.sql_policy.sqlglot.parse", parsed)
    with pytest.raises(ContestError) as caught:
        parse_readonly("SELECT CAST('sales' AS unknown_type)")
    assert caught.value.code == "SQL_REJECTED"


@pytest.mark.parametrize("projection", ["CAST(id AS BIGINT)", "CAST(amount AS NUMERIC)", "CAST('2024-01-01' AS DATE)"])
def test_native_allowed_casts_remain_valid(source, projection):
    """允许集中的真实内置类型仍可限定到授权表，不因兼容修复全部禁用CAST。"""
    query = prepare_sql(f"SELECT {projection} FROM sales", source, LinkResult(tables={"sales"}), {})
    assert '"business"."sales"' in query and query.endswith('AS "_contest_result" LIMIT 501')


def test_star_is_expanded_to_allowed_columns_and_execution_is_bounded(source):
    """实际执行文本使用授权Schema及列，外包限制是第501行探测。"""
    query = prepare_sql("SELECT * FROM sales", source, LinkResult(tables={"sales"}), {})
    assert '"business"."sales"' in query and '"amount"' in query
    assert query.endswith('AS "_contest_result" LIMIT 501')


def test_confirmed_filter_must_be_top_level_and_not_or_bypass(source):
    """确认期间不能丢失、改值或藏在可绕过的OR分支。"""
    catalog = source.model_copy(deep=True)
    catalog.conditions = [ConditionSpec(slot="year", table="sales", column="year", label="确认年度",
                                         value_type="integer", required=True)]
    linked = LinkResult(tables={"sales"})
    valid = prepare_sql("SELECT COUNT(*) FROM sales WHERE year=2024", catalog, linked, {"year": "2024"})
    assert "2024" in valid
    for query in ("SELECT COUNT(*) FROM sales", "SELECT COUNT(*) FROM sales WHERE year=2023",
                  "SELECT COUNT(*) FROM sales WHERE year=2024 OR 1=1"):
        with pytest.raises(ContestError):
            prepare_sql(query, catalog, linked, {"year": "2024"})


def test_join_requires_full_selected_key_and_new_right_alias(source):
    """有效外键可连接，猜键/CROSS/连接未涉及新右表均拒绝。"""
    linked = SchemaLinker().link(source, "统计销售额客户", {})
    valid = "SELECT SUM(s.amount), c.name FROM sales s JOIN customers c ON s.customer_id=c.id GROUP BY c.name"
    assert "JOIN" in prepare_sql(valid, source, linked, {})
    for query in (valid.replace("s.customer_id", "s.id"), valid.replace("s.customer_id=c.id", "s.customer_id=s.id"),
                  "SELECT s.amount FROM sales s CROSS JOIN customers c"):
        with pytest.raises(ContestError):
            prepare_sql(query, source, linked, {})


def test_composite_foreign_key_cannot_be_partially_used(source):
    """复合关系必须包含全部列对，不以第一对代替完整连接。"""
    catalog = source.model_copy(deep=True)
    catalog.tables[1].columns["year"] = "int4"
    catalog.foreign_keys[0].pairs.append(("year", "year"))
    linked = LinkResult(tables={"sales", "customers"}, keys=catalog.foreign_keys)
    with pytest.raises(ContestError):
        prepare_sql("SELECT s.amount FROM sales s JOIN customers c ON s.customer_id=c.id", catalog, linked, {})
    assert "JOIN" in prepare_sql("SELECT s.amount FROM sales s JOIN customers c "
                                 "ON s.customer_id=c.id AND s.year=c.year", catalog, linked, {})


def test_case_sensitive_authorization_is_preserved(source):
    """引号内真实大小写须保留，不能把另一个同名小写对象当授权表。"""
    catalog = source.model_copy(deep=True)
    catalog.schema_name = "Business"
    catalog.tables = [catalog.tables[0].model_copy(update={"name": "Sales", "columns": {"Amount": "numeric"}})]
    catalog.aliases, catalog.foreign_keys = {}, []
    linked = LinkResult(tables={"Sales"})
    assert '"Business"."Sales"' in prepare_sql('SELECT "Amount" FROM "Sales"', catalog, linked, {})
    with pytest.raises(ContestError):
        prepare_sql("SELECT amount FROM sales", catalog, linked, {})


def test_normalization_does_not_match_empty_word():
    """单字母复数规则不能产生匹配任意表的空Token。"""
    assert "" not in tokens("s")
