"""psycopg边界与结果编码用例，连接器完全隔离，不执行真实数据库查询。"""

from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
from pydantic import SecretStr

from contest_backend.errors import ContestError
from contest_backend.execution import SQLExecutor, encode_cell


class StubCursor:
    """只记录执行器协议调用，不解析或执行SQL。"""

    def __init__(self, *, failure=None, count=1, catalog=None):
        """可在用户SQL边界模拟数据库错误或第501行；目录查询保持独立。"""
        self.commands, self.failure, self.count = [], failure, count
        self.description = [SimpleNamespace(name="n", type_code=20)]
        self.catalog = catalog or []

    def __enter__(self):
        """模拟上下文管理器返回当前游标。"""
        return self

    def __exit__(self, *args):
        """不吞异常，交给执行器处理。"""
        return False

    def execute(self, query, parameters=None):
        """仅预定用户SQL触发错误，配置和目录命令不会访问数据库。"""
        self.commands.append((query, parameters))
        if query == "SELECT actual" and self.failure:
            raise self.failure

    def fetchone(self):
        """返回隔离计划，不冒充真实PostgreSQL成本。"""
        return [{"Plan": {"Total Cost": 1}}],

    def fetchmany(self, maximum):
        """最多生成指定数量内存行，断言500行显示与501行探测。"""
        return [(index,) for index in range(min(self.count, maximum))]

    def fetchall(self):
        """返回测试指定的目录行集。"""
        return self.catalog


class StubConnection:
    """记录只读属性和回滚次数的连接边界。"""

    def __init__(self, cursor):
        """注册一个已知测试结果类型，不查询真实OID。"""
        self.current_cursor, self.rollbacks, self.read_only = cursor, 0, False
        self.adapters = SimpleNamespace(types={20: SimpleNamespace(name="int8")})

    def __enter__(self):
        """返回连接，不启动任何服务。"""
        return self

    def __exit__(self, *args):
        """保留上抛错误供生产映射代码处理。"""
        return False

    def cursor(self):
        """提供同次游标对象。"""
        return self.current_cursor

    def rollback(self):
        """记录回滚，测试确认执行器不提交用户查询。"""
        self.rollbacks += 1


class CatalogCursor(StubCursor):
    """按元数据查询类型提供已授权基础表及外键，允许注入目录不一致。"""

    def __init__(self, source, *, writable=False, missing_key=False, hidden_table=False):
        """保存测试目录；不连接或创建数据库。"""
        super().__init__()
        self.source, self.writable, self.missing_key, self.hidden_table = source, writable, missing_key, hidden_table

    def fetchone(self):
        """只读角色无高权限、无Schema创建权，写表权限可作为拒绝反例。"""
        query = self.commands[-1][0]
        if "pg_roles" in query:
            return False, False, False, False, False
        if "has_schema_privilege" in query:
            return (False,)
        if "has_table_privilege" in query:
            return self.writable, self.writable
        return (True,)

    def fetchall(self):
        """返回目录列或真实约束列对，缺失约束不能冒充可靠连接依据。"""
        query, params = self.commands[-1]
        if "pg_constraint" in query:
            return [] if self.missing_key else [("actual_fk", "customer_id", "id")]
        table = next(item for item in self.source.tables if item.name == params[1])
        return [] if self.hidden_table else [("r", name, kind, "pg_catalog") for name, kind in table.columns.items()]


def executor_for(settings, monkeypatch, *, cursor=None):
    """注入无网络连接；本组只验证执行协议，目录权限另行测试。"""
    connection = StubConnection(cursor or StubCursor())

    def connector(url, **options):
        """保留调用选项以断言超时设置，不使用DSN联网。"""
        connection.options = options
        return connection

    def verified(cursor, source):
        """本组把目录验证隔离为前置步骤，不声明真实目录已核验。"""
        return None

    config = settings.model_copy(update={"database_urls": {"readonly": SecretStr("postgresql://unit.invalid/db")}})
    executor = SQLExecutor(config, connector=connector)
    monkeypatch.setattr(executor, "verify_catalog", verified)
    return executor, connection


def test_readonly_resource_limits_rollback_and_truncation(source, settings, monkeypatch):
    """同次连接只读、限时/禁并行，真实501个隔离输入才标截断。"""
    cursor = StubCursor(count=501)
    executor, connection = executor_for(settings, monkeypatch, cursor=cursor)
    columns, rows, truncated = executor.execute("SELECT actual", source)
    assert connection.read_only is True and connection.rollbacks == 1
    assert connection.options["connect_timeout"] == settings.connect_timeout_seconds
    commands = " ".join(query for query, _ in cursor.commands)
    assert "statement_timeout" in commands and "work_mem" in commands and "max_parallel_workers_per_gather" in commands
    assert len(rows) == 500 and truncated and columns[0].data_type == "int8"


@pytest.mark.parametrize("failure,code,execution", [
    (psycopg.errors.QueryCanceled("PRIVATE SQL"), "SQL_EXECUTION_FAILED", "failed"),
    (psycopg.OperationalError("PRIVATE DSN"), "EXECUTION_UNKNOWN", "unknown"),
    (psycopg.errors.InsufficientPrivilege("PRIVATE TABLE"), "PERMISSION_DENIED", "failed"),
])
def test_database_exception_mapping_is_safe(source, settings, monkeypatch, failure, code, execution):
    """确定超时/权限失败与断连未知分开；错误中不保留原SQL、DSN或表名。"""
    executor, connection = executor_for(settings, monkeypatch, cursor=StubCursor(failure=failure))
    with pytest.raises(ContestError) as caught:
        executor.execute("SELECT actual", source)
    assert caught.value.code == code and caught.value.execution == execution
    assert "PRIVATE" not in str(caught.value) and connection.rollbacks == 1


def test_unavailable_connection_does_not_claim_query_was_sent(source, settings):
    """建连前失败没有执行凭证，也不能以空表冒充成功。"""
    def unavailable(url, **options):
        """模拟不可用连接，不实际联网。"""
        raise psycopg.OperationalError("PRIVATE DSN")

    config = settings.model_copy(update={"database_urls": {"readonly": SecretStr("postgresql://unit.invalid/db")}})
    with pytest.raises(ContestError) as caught:
        SQLExecutor(config, connector=unavailable).execute("SELECT actual", source)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE" and caught.value.execution == "not_run"


@pytest.mark.parametrize("options", [{"writable": True}, {"missing_key": True}, {"hidden_table": True}])
def test_catalog_rejects_write_role_missing_real_key_or_hidden_table(source, settings, options):
    """真实目录核验拒绝可写角色、伪造外键及不存在的授权基础表。"""
    with pytest.raises(ContestError):
        SQLExecutor(settings).verify_catalog(CatalogCursor(source, **options), source)


def test_catalog_checks_full_declared_key_against_actual_constraint(source, settings):
    """有效目录可通过边界，核验只使用固定Schema和表名参数。"""
    cursor = CatalogCursor(source)
    SQLExecutor(settings).verify_catalog(cursor, source)
    query, params = cursor.commands[-1]
    assert "k.convalidated" in query and "p.pos" in query
    assert params == ("business", "business", "sales", "customers")


def test_lossless_scalar_encoding_and_unsupported_result():
    """大数/Decimal不损精度，NULL/布尔保留，复杂对象与非有限数明确拒绝。"""
    assert encode_cell(2**63 - 1) == "9223372036854775807"
    assert encode_cell(Decimal("123.4500")) == "123.4500"
    assert encode_cell(None) is None and encode_cell(True) is True
    for value in (float("nan"), Decimal("Infinity"), {"private": "payload"}):
        with pytest.raises(ContestError):
            encode_cell(value)
