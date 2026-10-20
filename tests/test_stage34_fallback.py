"""阶段34主备逐请求记账回归；仅用户运行，全部HTTP使用内存Transport。"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.services import llm
from scripts import stage34_evaluation as tool
from scripts.stage33_common import read_json
from scripts.stage34_runtime import Runtime
from test_llm_service import envelope, settings
from test_stage34_evaluation import fixture_plan


@pytest.fixture
def configuration(monkeypatch):
    """明确两方独立Mock配置，隔离用户环境，绝不读取本地密钥。"""
    for name in Settings.model_fields:
        if name.startswith(("llm_", "deepseek_", "custom_llm_")) or name == "rag_max_context_chars":
            monkeypatch.delenv(name.upper(), raising=False)
    return settings(llm_provider="custom", llm_fallback_enabled=True, llm_fallback_provider="deepseek")


class ProviderRuntime(Runtime):
    """保留真实Runtime工厂/主备策略/计数边界，只替代本地检索和来源查询。"""

    def __init__(self, configuration):
        """构造不加载模型的运行实例，用计数证明预算/策略拒绝发生在检索前。"""
        super().__init__(configuration)
        self.snapshot_checks = 0

    def identity(self):
        """只使用安全提供方/模型身份，不调用真实模型。"""
        return {"provider": self.llm.provider, "model": self.llm.model_name, **self.cloud_policy()}

    def validate_snapshot(self, plan):
        """模拟只读快照匹配，不连接数据库。"""
        self.snapshot_checks += 1

    def query(self, kb_id, question, *, doc_ids, before_cloud):
        """直接调用生产同一计费工厂；Prompt为内存字符串，未构造文档样本。"""
        self.stage, self.timings, self.raw_output = "input", {}, None
        with llm.llm_cloud_call(allow_cloud=True):
            self.llm_for_case(before_cloud).complete("PRIVATE_SYSTEM", question)
        return {"answer": {"status": "insufficient_evidence", "answer": "证据不足",
                           "insufficient_evidence": True, "citations": []},
            "source_check": {"status": "NOT_APPLICABLE"}, "timings_seconds": {},
            "raw_llm_output": self.raw_output, "final_evidence": []}


def mock_factory(monkeypatch, handler):
    """注入内存传输并返回原工厂供切换Transport；主备判断仍执行生产代码。"""
    original = llm.create_llm_service

    def create(config, **kwargs):
        """兼容Runtime初始预检与逐题代理，不允许真实网络传输。"""
        return original(config, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm, "create_llm_service", create)
    return original


@pytest.mark.parametrize("status,enabled,providers,error", [
    (200, True, ["custom"], None),
    (503, True, ["custom", "deepseek"], None),
    (401, True, ["custom"], "LLM_AUTH"),
    (503, False, ["custom"], "LLM_HTTP"),
])
def test_runtime_honors_config_and_persists_each_provider_before_http(
        tmp_path, monkeypatch, configuration, status, enabled, providers, error):
    """503才按配置切换；请求前磁盘已有各方意图，原消息和独立凭据不变。"""
    configuration = configuration.model_copy(update={"llm_fallback_enabled": enabled})
    plan, requests = fixture_plan(1, fallback=enabled), []

    def respond(request):
        """检查意图先于每次HTTP，全部响应只在内存生成。"""
        requests.append(request)
        record = read_json(tool.case_path(tmp_path, plan["cases"][0]))
        attempt = record["llm_attempts"][-1]
        assert len(record["llm_attempts"]) == len(requests)
        assert attempt["status"] == "STARTED" and attempt["provider"] == providers[len(requests) - 1]
        return (httpx.Response(status, text="PRIVATE_UPSTREAM")
                if request.url.host == "compatible.example" and status != 200
                else httpx.Response(200, json=envelope()))

    mock_factory(monkeypatch, respond)
    runtime = ProviderRuntime(configuration)
    assert runtime.settings.llm_fallback_enabled is enabled
    assert configuration.llm_fallback_enabled is enabled
    record = next(iter(tool.execute(tmp_path, plan, runtime, max_cloud_calls=2, limit=1).values()))
    assert [a["provider"] for a in record["llm_attempts"]] == providers
    assert record.get("error_code") == error
    assert record["actual_provider"] == (None if error else providers[-1])
    if len(requests) == 2:
        first, second = (json.loads(r.content) for r in requests)
        assert first["messages"] == second["messages"]
        assert first["model"] != second["model"]
        assert requests[0].headers["authorization"] != requests[1].headers["authorization"]
        assert record["llm_attempts"][0]["http_status"] == 503
    result = tool.summarize(plan, {record["case_id"]: record}, [])
    assert result["llm_attempts_recorded"] == len(providers)
    assert result["fallback_cases"] == int(len(providers) == 2)
    assert result["llm_requests_by_provider"] == dict.fromkeys(providers, 1)


@pytest.mark.parametrize("fault,code", [(503, "DEEPSEEK_HTTP"), ("timeout", "DEEPSEEK_NETWORK")])
def test_backup_failure_retains_official_cause_and_stops_without_third_request(
        tmp_path, monkeypatch, configuration, fault, code):
    """备用失败保持原官方安全原因和失败阶段，不变成通用错误或继续下一题收费。"""
    requests = []

    def respond(request):
        """主请求503，备用分别模拟503和读取超时。"""
        requests.append(request)
        if request.url.host == "api.deepseek.com" and fault == "timeout":
            raise httpx.ReadTimeout("PRIVATE_SECRET", request=request)
        return httpx.Response(503, text="PRIVATE_UPSTREAM")

    mock_factory(monkeypatch, respond)
    plan = fixture_plan(2, fallback=True)
    records = tool.execute(tmp_path, plan, ProviderRuntime(configuration), max_cloud_calls=4, limit=2)
    assert len(requests) == 2 and len(records) == 1
    record = next(iter(records.values()))
    assert record["status"] == "FAILED" and record["error_code"] == code
    assert record["failure_stage"] == "llm" and record["actual_provider"] is None
    assert record["llm_attempts"][-1]["provider"] == "deepseek"
    assert record["llm_attempts"][-1]["error_code"] == code
    assert "PRIVATE" not in json.dumps(record)


def test_fallback_budget_and_stale_one_request_plan_rejected_before_models(
        tmp_path, monkeypatch, configuration):
    """开启备用必须预留两次；旧关闭备用计划不能静默用新配置发送请求。"""
    requests = []
    mock_factory(monkeypatch, lambda r: requests.append(r))
    runtime = ProviderRuntime(configuration)
    with pytest.raises(tool.Stage34Error, match="CLOUD_BUDGET"):
        tool.execute(tmp_path, fixture_plan(1, fallback=True), runtime, max_cloud_calls=1, limit=1)
    with pytest.raises(tool.Stage34Error, match="LLM_POLICY_CHANGED"):
        tool.execute(tmp_path, fixture_plan(1), runtime, max_cloud_calls=2, limit=1)
    assert requests == [] and runtime.snapshot_checks == 0
    assert not (tmp_path / "runtime.json").exists()


def test_backup_intent_write_failure_prevents_second_http(tmp_path, monkeypatch, configuration):
    """主请求失败后第二次意图落盘失败，则不发送备用且不伪记第二次已调用。"""
    requests, write = [], tool.write_json

    def respond(request):
        """只允许被批准且已记账的主请求到达内存Transport。"""
        requests.append(request)
        return httpx.Response(503)

    def fail_second_intent(path, value):
        """只阻断第二次意图，其余结果仍可保存以核对收费记录。"""
        if len(value.get("llm_attempts", [])) == 2:
            raise OSError("PRIVATE_PATH")
        write(path, value)

    mock_factory(monkeypatch, respond)
    monkeypatch.setattr(tool, "write_json", fail_second_intent)
    plan = fixture_plan(2, fallback=True)
    records = tool.execute(tmp_path, plan, ProviderRuntime(configuration), max_cloud_calls=4, limit=2)
    record = next(iter(records.values()))
    assert len(requests) == len(records) == len(record["llm_attempts"]) == 1
    assert record["failure_stage"] == "llm" and record["status"] == "FAILED"
    assert record["llm_attempts"][0]["http_status"] == 503


def test_interrupted_backup_reserves_two_and_resume_does_not_replay_it(
        tmp_path, monkeypatch, configuration):
    """备用中断后保留两次意图并占额；续跑只能执行未开始题，不能重发该题。"""
    requests = []

    def interrupted(request):
        """主失败后，在第二次意图落盘后的HTTP边界模拟进程中断。"""
        requests.append(request)
        if request.url.host == "api.deepseek.com":
            raise KeyboardInterrupt
        return httpx.Response(503)

    original = mock_factory(monkeypatch, interrupted)
    plan = fixture_plan(2, fallback=True)
    with pytest.raises(KeyboardInterrupt):
        tool.execute(tmp_path, plan, ProviderRuntime(configuration), max_cloud_calls=2, limit=1)
    first = read_json(tool.case_path(tmp_path, plan["cases"][0]))
    assert first["status"] == "STARTED" and len(first["llm_attempts"]) == 2
    assert [a["status"] for a in first["llm_attempts"]] == ["FAILED", "STARTED"]
    runtime = ProviderRuntime(configuration)
    with pytest.raises(tool.Stage34Error, match="CLOUD_BUDGET"):
        tool.execute(tmp_path, plan, runtime, max_cloud_calls=3, limit=1)
    assert runtime.snapshot_checks == 0 and len(requests) == 2

    def success(request):
        """新题主请求一次成功，旧中断题不会进入Transport。"""
        requests.append(request)
        return httpx.Response(200, json=envelope())

    # 先恢复原工厂，再注入新Transport，避免叠加两层Mock工厂。
    monkeypatch.setattr(llm, "create_llm_service", original)
    mock_factory(monkeypatch, success)
    records = tool.execute(tmp_path, plan, ProviderRuntime(configuration), max_cloud_calls=4, limit=2)
    assert len(requests) == 3 and records[first["case_id"]] == first
    result = tool.summarize(plan, records, [])
    assert result["llm_attempts_recorded"] == result["llm_budget_reserved"] == 3
    assert result["llm_attempts_uncertain"] == 1


def test_actual_provider_is_per_case_and_does_not_leak_after_fallback(
        tmp_path, monkeypatch, configuration):
    """同一Runtime先备用成功、后主请求成功，各题实际提供方和累计三次请求分开。"""
    requests = []

    def respond(request):
        """只让首个主请求失败，后续请求均返回相同合法内存响应。"""
        requests.append(request)
        return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json=envelope())

    mock_factory(monkeypatch, respond)
    plan = fixture_plan(2, fallback=True)
    records = tool.execute(tmp_path, plan, ProviderRuntime(configuration), max_cloud_calls=4, limit=2)
    assert [r["actual_provider"] for r in records.values()] == ["deepseek", "custom"]
    result = tool.summarize(plan, records, [])
    assert result["llm_attempts_recorded"] == result["llm_budget_reserved"] == 3
    assert result["llm_requests_by_provider"] == {"custom": 2, "deepseek": 1}
    assert result["llm_response_cases_by_provider"] == {"custom": 1, "deepseek": 1}
