"""阶段2隔离用例的纯内存夹具；仅编写，不由Codex执行或访问真实资源。"""

from uuid import uuid4

import pytest

from contracts.contest_v1 import AgentRequest, SQLColumn, SQLScope
from contest_backend.config import Settings
from contest_backend.registry import Registry, SourceSpec
from contest_backend.service import ContestService


class RecordingLLM:
    """测试专用候选生成器，记录确认条件，不构成生产模型替代实现。"""

    def __init__(self, sql="SELECT COUNT(*) AS total FROM sales", error=None):
        """保存预定候选及安全异常，测试时不联网。"""
        self.sql, self.error, self.messages = sql, error, []

    def complete(self, messages, grant):
        """模拟一次已预留预算的调用，记录真实传入的提示。"""
        grant.reserve(100)
        grant.attempted_calls += 1
        self.messages.append(messages)
        if self.error:
            raise self.error
        return self.sql


class RecordingExecutor:
    """测试专用执行边界，所有入参只保存在内存断言中。"""

    def __init__(self, errors=(), rows=None):
        """可指定每次错误及表格，禁止连接数据库。"""
        self.errors, self.calls = list(errors), []
        self.rows = [["7"]] if rows is None else rows

    def execute(self, query, source):
        """记录最终SQL并返回隔离测试表格；不用此结果宣称真实查询通过。"""
        self.calls.append((query, source))
        if self.errors:
            raise self.errors.pop(0)
        return [SQLColumn(name="total", data_type="int8")], self.rows, False


@pytest.fixture
def source():
    """提供含字段歧义及真实约束描述的测试目录，不创建实际表。"""
    return SourceSpec(db_id="demo", sub_db="sales", connection_id="readonly", schema_name="business",
        tables=[{"name": "sales", "columns": {"id": "int4", "amount": "numeric", "other_amount": "numeric",
                                              "customer_id": "int4", "year": "int4"}},
                {"name": "customers", "columns": {"id": "int4", "name": "text"}}],
        aliases={"销售额": ["sales.amount"], "收入": ["sales.amount", "sales.other_amount"], "客户": ["customers"]},
        foreign_keys=[{"id": "sales_customer", "from_table": "sales", "to_table": "customers",
                       "pairs": [["customer_id", "id"]]}])


@pytest.fixture
def settings():
    """显式清空可能来自本机环境的提供方/连接设置，测试不使用真实密钥。"""
    return Settings(_env_file=None, api_token="unit-test-token", database_urls={}, llm_api_key="",
                    llm_api_base_url="", llm_model="", llm_provider_id="contest")


@pytest.fixture
def build_service(source, settings):
    """返回可配置会话构造器；每个用例独立目录、许可和确认条件。"""
    def build(*, catalog=None, llm=None, executor=None):
        """默认授权仅为测试内存许可，不触发任何真实模型或SQL。"""
        model = llm if llm is not None else RecordingLLM()
        database = executor if executor is not None else RecordingExecutor()
        service = ContestService(settings, registry=Registry(sources=[catalog or source]), llm=model, executor=database)
        session, token = service.sessions.create(catalog or source)
        policy = service.issue_consent(session, provider_id="contest", max_calls=1,
                                       max_total_tokens=32768, allow_sql_repair=False)
        return service, session, token, policy, model, database
    return build


def request_for(session, policy=None, **changes):
    """绑定固定原问题/范围构造新操作身份；可显式使用默认零云许可。"""
    data = {"session_id": session.id, "client_request_id": uuid4(), "question": "统计sales",
            "intent": "sql", "sql_scope": SQLScope(db_id=session.source.db_id, sub_db=session.source.sub_db), **changes}
    if policy is not None:
        data["cloud"] = policy
    return AgentRequest(**data)
