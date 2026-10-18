"""统一授权目录：连接标识、Schema、表列、别名、复合外键及显式业务条件。"""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, model_validator

from contracts.contest_v1 import ContractModel, Identifier, SQLScope
from contest_backend.errors import ContestError

NAME = r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,62}$"
TYPES = frozenset({"bool", "int2", "int4", "int8", "numeric", "float4", "float8", "text", "varchar", "bpchar",
                   "date", "time", "timetz", "timestamp", "timestamptz", "uuid"})


class TableSpec(ContractModel):
    """只允许可信基础表及明确列类型；不接受任意DDL、表达式或视图。"""

    name: StrictStr = Field(pattern=NAME)
    columns: dict[str, str] = Field(min_length=1, max_length=128)


class ForeignKey(ContractModel):
    """保留复合键列对，不按同名列猜关系或丢弃多约束。"""

    id: Identifier
    from_table: StrictStr
    to_table: StrictStr
    pairs: list[tuple[str, str]] = Field(min_length=1, max_length=16)


class ConditionSpec(ContractModel):
    """管理员登记的可澄清条件；仅支持可机械核对的直接列比较。"""

    slot: Identifier
    table: StrictStr
    column: StrictStr
    label: StrictStr = Field(min_length=1, max_length=200)
    operator: Literal["eq", "gte", "lte"] = "eq"
    value_type: Literal["text", "integer", "decimal", "date"] = "text"
    triggers: list[StrictStr] = Field(default_factory=list, max_length=16)
    required: StrictBool = False


class SourceSpec(ContractModel):
    """一个db_id/sub_db对应一个固定数据库连接及Schema，授权来自服务端文件。"""

    db_id: Identifier
    sub_db: Identifier
    connection_id: Identifier
    schema_name: StrictStr = Field(pattern=NAME)
    tables: list[TableSpec] = Field(min_length=1, max_length=32)
    aliases: dict[str, list[str]] = Field(default_factory=dict, max_length=128)
    foreign_keys: list[ForeignKey] = Field(default_factory=list, max_length=128)
    conditions: list[ConditionSpec] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_catalog(self):
        """拒绝失效别名、系统Schema、虚构列/外键与重复身份，整个目录失败关闭。"""
        tables = {item.name: item.columns for item in self.tables}
        if (len(tables) != len(self.tables) or self.schema_name.lower() in {"pg_catalog", "information_schema"}
                or self.schema_name.lower().startswith("pg_")
                or len({item.id for item in self.foreign_keys}) != len(self.foreign_keys)
                or len({item.slot for item in self.conditions}) != len(self.conditions)):
            raise ValueError("授权目录身份无效")
        for columns in tables.values():
            if any(not re.fullmatch(NAME, name) or kind not in TYPES for name, kind in columns.items()):
                raise ValueError("授权列或类型无效")
        targets = set(tables) | {f"{table}.{column}" for table, columns in tables.items() for column in columns}
        for alias, values in self.aliases.items():
            if not alias.strip() or len(alias) > 128 or not values or len(values) > 20 or not set(values) <= targets:
                raise ValueError("别名不属于授权目录")
        for key in self.foreign_keys:
            if (key.from_table not in tables or key.to_table not in tables or key.from_table == key.to_table
                    or any(a not in tables[key.from_table] or b not in tables[key.to_table] for a, b in key.pairs)
                    or len({a for a, _ in key.pairs}) != len(key.pairs)
                    or len({b for _, b in key.pairs}) != len(key.pairs)):
                raise ValueError("外键列对无效")
        for condition in self.conditions:
            if (condition.table not in tables or condition.column not in tables[condition.table]
                    or condition.slot in {"intent", "source_target"} or condition.slot.startswith(("schema_", "join_"))):
                raise ValueError("业务条件不属于授权目录")
        return self

    def fingerprint(self) -> str:
        """对完整授权快照计算摘要，用于会话固定范围和引用依据。"""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    def table_map(self) -> dict[str, dict[str, str]]:
        """返回当前源明确允许的表列，不向其他数据源回退。"""
        return {item.name: dict(item.columns) for item in self.tables}


class Registry(ContractModel):
    """全服务唯一注册表；空目录是真实不可用状态，不预置演示数据库。"""

    version: Literal[1] = 1
    sources: list[SourceSpec] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_sources(self):
        """同一对来源标识只能有一个连接/Schema解释。"""
        if len({(item.db_id, item.sub_db) for item in self.sources}) != len(self.sources):
            raise ValueError("数据源重复")
        return self

    @classmethod
    def load(cls, path: Path):
        """只读受限本地配置，错误不暴露路径、文件正文或上游异常。"""
        try:
            if path.stat().st_size > 1048576:
                raise ValueError("目录超限")
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            raise ContestError("DEPENDENCY_UNAVAILABLE", 503) from None

    def resolve(self, scope: SQLScope) -> SourceSpec:
        """客户端只选择已登记的精确范围，找不到时拒绝而非切换默认库。"""
        for source in self.sources:
            if (source.db_id, source.sub_db) == (scope.db_id, scope.sub_db):
                return source.model_copy(deep=True)
        raise ContestError("SCOPE_INVALID", 403)
