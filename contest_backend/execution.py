"""psycopg3只读执行适配：短连接、角色/元数据检查、资源上限及安全错误。"""

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

import psycopg

from contracts.contest_v1 import SQLColumn
from contest_backend.config import Settings
from contest_backend.errors import ContestError
from contest_backend.registry import SourceSpec


def encode_cell(value):
    """将有限标量无损传给JSON；禁止隐式字符串化复杂对象或截断真实值。"""
    if value is None or type(value) is bool:
        return value
    if type(value) is int or isinstance(value, Decimal) and value.is_finite():
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if type(value) is str and len(value) <= 100000:
        return value
    raise ContestError("RESULT_INVALID", 502, execution="succeeded")


class SQLExecutor:
    """只有经过SQLPolicy的语句进入此层；执行前再次检查固定来源和数据库角色。"""

    def __init__(self, settings: Settings, connector=None):
        """connector仅用于编写隔离单元用例，生产默认使用真实psycopg连接。"""
        self.settings, self.connector = settings, connector or psycopg.connect

    def verify_catalog(self, cursor, source: SourceSpec) -> None:
        """核验实际基础表/列类型与只读角色，避免陈旧目录、视图或自定义类型绕过。"""
        cursor.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                       "FROM pg_catalog.pg_roles WHERE rolname = current_user")
        role = cursor.fetchone()
        if role is None or any(role):
            raise ContestError("PERMISSION_DENIED", 403)
        cursor.execute("SELECT pg_catalog.has_schema_privilege(current_user, %s, 'CREATE')", (source.schema_name,))
        if cursor.fetchone()[0]:
            raise ContestError("PERMISSION_DENIED", 403)
        for table in source.tables:
            cursor.execute("SELECT c.relkind, a.attname, t.typname, tn.nspname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                "JOIN pg_catalog.pg_type t ON t.oid=a.atttypid "
                "JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace "
                "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhparent=c.oid OR i.inhrelid=c.oid)",
                (source.schema_name, table.name))
            actual = {name: (kind, typename, namespace) for kind, name, typename, namespace in cursor.fetchall()}
            if any(actual.get(name) != ("r", datatype, "pg_catalog")
                   for name, datatype in table.columns.items()):
                raise ContestError("SCOPE_INVALID", 403)
            qualified = '"' + source.schema_name + '"."' + table.name + '"'
            cursor.execute("SELECT pg_catalog.has_table_privilege(current_user, %s, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES'), "
                           "pg_catalog.has_any_column_privilege(current_user, %s, 'INSERT,UPDATE,REFERENCES')", (qualified, qualified))
            if any(cursor.fetchone()):
                raise ContestError("PERMISSION_DENIED", 403)
            for column in table.columns:
                cursor.execute("SELECT pg_catalog.has_column_privilege(current_user, %s, %s, 'SELECT')", (qualified, column))
                if not cursor.fetchone()[0]:
                    raise ContestError("PERMISSION_DENIED", 403)
        for key in source.foreign_keys:
            # 目录只描述真实且已验证的约束，不能由同名列或管理员标签冒充数据库外键。
            cursor.execute("SELECT k.conname, a.attname, b.attname FROM pg_catalog.pg_constraint k "
                "JOIN pg_catalog.pg_class f ON f.oid=k.conrelid "
                "JOIN pg_catalog.pg_class t ON t.oid=k.confrelid "
                "JOIN pg_catalog.pg_namespace fn ON fn.oid=f.relnamespace "
                "JOIN pg_catalog.pg_namespace tn ON tn.oid=t.relnamespace "
                "JOIN LATERAL pg_catalog.unnest(k.conkey) WITH ORDINALITY p(fnum, pos) ON true "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=f.oid AND a.attnum=p.fnum "
                "JOIN pg_catalog.pg_attribute b ON b.attrelid=t.oid AND b.attnum=k.confkey[p.pos::integer] "
                "WHERE k.contype='f' AND k.convalidated AND fn.nspname=%s AND tn.nspname=%s "
                "AND f.relname=%s AND t.relname=%s ORDER BY k.oid, p.pos",
                (source.schema_name, source.schema_name, key.from_table, key.to_table))
            constraints = {}
            for identity, left, right in cursor.fetchall():
                constraints.setdefault(identity, []).append((left, right))
            if key.pairs not in constraints.values():
                raise ContestError("SCOPE_INVALID", 403)

    def execute(self, query: str, source: SourceSpec):
        """执行真实SQL且只读回滚；超时/断连区分确定失败与未知，不进行重试。"""
        secret = self.settings.database_urls.get(source.connection_id)
        if secret is None or not secret.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ContestError("DEPENDENCY_UNAVAILABLE", 503)
        sent, completed, planning = False, False, False
        try:
            with self.connector(secret.get_secret_value(), connect_timeout=self.settings.connect_timeout_seconds,
                                autocommit=False) as connection:
                connection.read_only = True
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL search_path TO pg_catalog")
                        cursor.execute("SELECT pg_catalog.set_config('statement_timeout', %s, true)",
                                       (str(self.settings.statement_timeout_ms),))
                        cursor.execute("SET LOCAL lock_timeout TO 1000")
                        cursor.execute("SET LOCAL idle_in_transaction_session_timeout TO 5000")
                        cursor.execute("SET LOCAL work_mem TO '4MB'")
                        cursor.execute("SET LOCAL max_parallel_workers_per_gather TO 0")
                        self.verify_catalog(cursor, source)
                        planning = True
                        cursor.execute("EXPLAIN (FORMAT JSON) " + query)
                        plan = cursor.fetchone()[0]
                        if not isinstance(plan, list) or plan[0]["Plan"]["Total Cost"] > self.settings.max_plan_cost:
                            raise ContestError("SQL_REJECTED", 403)
                        sent = True
                        cursor.execute(query)
                        completed = True
                        if not cursor.description or len(cursor.description) > 256:
                            raise ContestError("RESULT_INVALID", 502, execution="succeeded")
                        # 类型OID来自同次真实游标，类型名由当前连接的PG类型注册表解析。
                        columns = [SQLColumn(name=item.name, data_type=connection.adapters.types.get(item.type_code).name)
                                   for item in cursor.description]
                        raw = cursor.fetchmany(501)
                        rows = [[encode_cell(value) for value in row] for row in raw[:500]]
                        size = len(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
                        if size > self.settings.max_result_bytes:
                            raise ContestError("RESULT_INVALID", 502, execution="succeeded")
                        return columns, rows, len(raw) > 500
                finally:
                    # 即便SELECT或元数据失败，也不提交会话状态；禁止隐式重放用户查询。
                    connection.rollback()
        except ContestError:
            raise
        except psycopg.Error as error:
            state = error.sqlstate
            if state == "57014":
                raise ContestError("SQL_EXECUTION_FAILED", 504, execution="failed" if sent else "not_run") from None
            if state == "42501":
                raise ContestError("PERMISSION_DENIED", 403, execution="failed" if sent else "not_run") from None
            if not state or state.startswith("08"):
                raise ContestError("EXECUTION_UNKNOWN" if sent else "DEPENDENCY_UNAVAILABLE", 503,
                                   execution="unknown" if sent else "not_run") from None
            raise ContestError("SQL_EXECUTION_FAILED", 502, execution="failed" if sent else "not_run",
                               repairable=(sent or planning) and state in {"42601", "42803", "42804"}) from None
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise ContestError("RESULT_INVALID", 502,
                               execution="succeeded" if completed else "unknown" if sent else "not_run") from None
