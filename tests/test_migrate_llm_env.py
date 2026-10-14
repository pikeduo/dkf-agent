"""仅使用假密钥字符串验证机械迁移，不读取用户.env、不运行配置脚本。"""

import pytest

from scripts.migrate_llm_env import decoded, migrate_text


def legacy_text(provider="sub2api"):
    """构造旧单提供方配置，注释中的官方假密钥与网关假密钥不同。"""
    return "\n".join([
        "APP_ENV=development", "# 模型与 LLM",
        "# DEEPSEEK_API_BASE_URL=https://api.deepseek.com", "# DEEPSEEK_API_KEY='official-fake-key'",
        "# DEEPSEEK_MODEL=deepseek-flash", f"DEEPSEEK_PROVIDER={provider}",
        "DEEPSEEK_API_BASE_URL=http://custom.example:8080/v1" if provider == "sub2api" else "DEEPSEEK_API_BASE_URL=https://api.deepseek.com",
        "DEEPSEEK_API_KEY='gateway-fake-key'" if provider == "sub2api" else "DEEPSEEK_API_KEY='official-fake-key'",
        "DEEPSEEK_MODEL=DeepSeek-V4.1-Flash", "DEEPSEEK_MAX_TOKENS=8192", "DEEPSEEK_TIMEOUT_SECONDS=180",
        "DEEPSEEK_ALLOW_INSECURE_HTTP=true", "RAG_MAX_CONTEXT_CHARS=60000", "", "MINERU_API_TOKEN=other-fake-key", "",
    ])


def active_values(text):
    """解码迁移结果的活动赋值，注释归档不参与新配置。"""
    values = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, raw = line.split("=", 1)
            values[key] = decoded(key, raw)
    return values


def test_sub2api_migration_preserves_both_keys_budgets_and_explicit_http_choice():
    """保留两把不同假密钥、原预算和既有HTTP授权，不改变其他服务配置。"""
    original = legacy_text()
    result, provider = migrate_text(original)
    values = active_values(result)
    assert provider == values["LLM_PROVIDER"] == "custom"
    assert values["DEEPSEEK_API_KEY"] == "official-fake-key"
    assert values["CUSTOM_LLM_API_KEY"] == "gateway-fake-key"
    assert values["DEEPSEEK_API_BASE_URL"] == "https://api.deepseek.com"
    assert values["CUSTOM_LLM_API_BASE_URL"] == "http://custom.example:8080/v1"
    assert values["CUSTOM_LLM_MAX_TOKENS"] == values["DEEPSEEK_MAX_TOKENS"] == "8192"
    assert values["CUSTOM_LLM_TIMEOUT_SECONDS"] == "180"
    assert values["CUSTOM_LLM_ALLOW_INSECURE_HTTP"] == "true"
    assert values["MINERU_API_TOKEN"] == "other-fake-key"
    assert "DEEPSEEK_PROVIDER" not in values and "DEEPSEEK_ALLOW_INSECURE_HTTP" not in values
    assert "# DEEPSEEK_API_KEY='gateway-fake-key'" in result
    assert migrate_text(result) == (result, "custom")


def test_official_migration_does_not_copy_official_secret_to_custom():
    """旧官方模式默认官方，不能把其密钥或HTTP开关送往自定义提供方。"""
    result, provider = migrate_text(legacy_text("official"))
    values = active_values(result)
    assert provider == "deepseek"
    assert values["DEEPSEEK_API_KEY"] == "official-fake-key"
    assert values["CUSTOM_LLM_API_KEY"] == ""
    assert values["CUSTOM_LLM_ALLOW_INSECURE_HTTP"] == "false"


@pytest.mark.parametrize("kind", ["missing_official_key", "duplicate_key", "duplicate_comment", "nonofficial_restore", "mixed_new"])
def test_ambiguous_migration_is_refused_without_returning_private_values(kind):
    """来源不唯一不猜测、不输出假密钥；纯函数失败不会写用户文件。"""
    text = legacy_text()
    if kind == "missing_official_key":
        text = text.replace("# DEEPSEEK_API_KEY='official-fake-key'\n", "")
    elif kind == "duplicate_key":
        text += "DEEPSEEK_API_KEY=another-fake\n"
    elif kind == "duplicate_comment":
        text += "# DEEPSEEK_API_KEY=another-fake\n"
    elif kind == "nonofficial_restore":
        text = text.replace("# DEEPSEEK_API_BASE_URL=https://api.deepseek.com", "# DEEPSEEK_API_BASE_URL=https://other.example")
    else:
        text += "CUSTOM_LLM_API_KEY=another-fake\n"
    with pytest.raises(ValueError) as failure:
        migrate_text(text)
    assert "fake-key" not in str(failure.value)
