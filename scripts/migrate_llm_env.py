"""机械迁移本地 LLM 配置；只改 .env，不加载业务、不联网、不输出配置值。"""

import argparse
from io import StringIO
from pathlib import Path
import re

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_URLS = {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
KEYS = (
    "LLM_PROVIDER", "DEEPSEEK_API_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
    "DEEPSEEK_MAX_TOKENS", "DEEPSEEK_TIMEOUT_SECONDS", "CUSTOM_LLM_API_BASE_URL",
    "CUSTOM_LLM_API_KEY", "CUSTOM_LLM_MODEL", "CUSTOM_LLM_MAX_TOKENS",
    "CUSTOM_LLM_TIMEOUT_SECONDS", "CUSTOM_LLM_ALLOW_INSECURE_HTTP", "RAG_MAX_CONTEXT_CHARS",
)
LEGACY = {"DEEPSEEK_PROVIDER", "DEEPSEEK_ALLOW_INSECURE_HTTP"}


def decoded(key, raw):
    """只解码用于比较的单项，保留原始赋值文本供迁移，不打印值。"""
    return dotenv_values(stream=StringIO(f"{key}={raw}\n"), interpolate=False).get(key)


def migrate_text(text: str) -> tuple[str, str]:
    """保留官方/网关密钥原值；来源不唯一、混合新旧配置或区域冲突时拒绝猜测。"""
    lines = text.splitlines()
    active, commented, indexes = {}, {}, []
    pattern = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)\s*=(.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match or match[2] not in set(KEYS) | LEGACY:
            continue
        indexes.append(index)
        mapping = commented if match[1] else active
        mapping.setdefault(match[2], []).append(match[3])
    if not indexes or any(len(values) != 1 for values in active.values()):
        raise ValueError("LLM_ENV_MIGRATION：缺少配置或存在重复活动变量，请人工检查（未修改文件）")
    values = {key: rows[0] for key, rows in active.items()}
    legacy = decoded("DEEPSEEK_PROVIDER", values.get("DEEPSEEK_PROVIDER", "official"))
    if "LLM_PROVIDER" in values:
        provider = decoded("LLM_PROVIDER", values["LLM_PROVIDER"])
        if provider not in {"deepseek", "custom"} or legacy == "sub2api" or not set(KEYS) <= values.keys():
            raise ValueError("LLM_ENV_MIGRATION：新旧配置混用或不完整，请人工检查（未修改文件）")
        return text, provider
    if any(key.startswith("CUSTOM_LLM_") for key in values) or legacy not in {"official", "sub2api"}:
        raise ValueError("LLM_ENV_MIGRATION：来源不明确，请人工检查（未修改文件）")
    output = {
        "LLM_PROVIDER": "deepseek", "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_API_KEY": "", "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_MAX_TOKENS": "2048", "DEEPSEEK_TIMEOUT_SECONDS": "60",
        "CUSTOM_LLM_API_BASE_URL": "http://45.129.8.116:8080/v1", "CUSTOM_LLM_API_KEY": "",
        "CUSTOM_LLM_MODEL": "DeepSeek-V4.1-Flash", "CUSTOM_LLM_MAX_TOKENS": "2048",
        "CUSTOM_LLM_TIMEOUT_SECONDS": "60", "CUSTOM_LLM_ALLOW_INSECURE_HTTP": "false",
        "RAG_MAX_CONTEXT_CHARS": "60000",
    }
    for key in KEYS:
        if key in values:
            output[key] = values[key]
    if legacy == "sub2api":
        # 必须有唯一的保留官方配置证明，不能把网关 Key 猜成官方 Key。
        for key in ("DEEPSEEK_API_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
            if len(commented.get(key, [])) != 1 or key not in values:
                raise ValueError("LLM_ENV_MIGRATION：官方保留配置不唯一或缺失，请人工检查（未修改文件）")
            output[key] = commented[key][0]
        if decoded("DEEPSEEK_API_BASE_URL", output["DEEPSEEK_API_BASE_URL"]) not in OFFICIAL_URLS:
            raise ValueError("LLM_ENV_MIGRATION：不能确认官方保留地址（未修改文件）")
        output["LLM_PROVIDER"] = "custom"
        for old, new in (("DEEPSEEK_API_BASE_URL", "CUSTOM_LLM_API_BASE_URL"),
                         ("DEEPSEEK_API_KEY", "CUSTOM_LLM_API_KEY"),
                         ("DEEPSEEK_MODEL", "CUSTOM_LLM_MODEL"),
                         ("DEEPSEEK_MAX_TOKENS", "CUSTOM_LLM_MAX_TOKENS"),
                         ("DEEPSEEK_TIMEOUT_SECONDS", "CUSTOM_LLM_TIMEOUT_SECONDS"),
                         ("DEEPSEEK_ALLOW_INSECURE_HTTP", "CUSTOM_LLM_ALLOW_INSECURE_HTTP")):
            if old in values:
                output[new] = values[old]
    elif decoded("DEEPSEEK_API_BASE_URL", output["DEEPSEEK_API_BASE_URL"]) not in OFFICIAL_URLS:
        raise ValueError("LLM_ENV_MIGRATION：旧官方配置含非官方地址，拒绝推测提供方（未修改文件）")

    first, last = min(indexes), max(indexes)
    for line in lines[first:last + 1]:
        match = pattern.match(line)
        if match and not match[1] and match[2] not in set(KEYS) | LEGACY:
            raise ValueError("LLM_ENV_MIGRATION：LLM 区域夹有其他活动配置，请人工整理（未修改文件）")
    block = ["# LLM 选择：deepseek=官方，custom=独立兼容API；切换后重启问答进程。"]
    for key in KEYS:
        if key == "DEEPSEEK_API_BASE_URL":
            block.append("# DeepSeek 官方配置：保留官方密钥与原有预算。")
        elif key == "CUSTOM_LLM_API_BASE_URL":
            block.append("# 自定义 API：独立密钥；HTTP 仅沿用此前明确授权，正式部署须 HTTPS。")
        elif key == "RAG_MAX_CONTEXT_CHARS":
            block.append("# 完整 Prompt 字符预算；超限不截断证据。")
        block.append(f"{key}={output[key]}")
    # 原配置整段注释归档，既不丢失真实密钥，也不让旧活动选择器继续生效。
    block.extend(["", "# 迁移前 LLM 配置归档（仅本地保留，不提交、不打印；以下均不生效）："])
    block.extend(line if line.lstrip().startswith("#") else "# " + line for line in lines[first:last + 1])
    ending = "\r\n" if "\r\n" in text else "\n"
    return ending.join(lines[:first] + block + lines[last + 1:]) + ending, output["LLM_PROVIDER"]


def main() -> int:
    """默认预览安全摘要；--apply 仅机械改写项目 .env，写入前确认源文件未变化。"""
    parser = argparse.ArgumentParser(description="迁移独立 LLM 提供方配置，不输出任何密钥或地址")
    parser.add_argument("--apply", action="store_true", help="明确应用本地 .env 文本迁移；默认不写入")
    args = parser.parse_args()
    path = ROOT / ".env"
    try:
        original = path.read_bytes()
        text = original.decode("utf-8-sig")
        result, provider = migrate_text(text)
        if args.apply and result != text:
            if path.read_bytes() != original:
                raise ValueError("LLM_ENV_MIGRATION：文件已变化，未写入")
            # 仅替换用户授权的文本配置，不执行任何安装、部署或业务验证。
            with path.open("wb") as target:
                target.write(result.encode("utf-8"))
        print("LLM 配置迁移：", "已应用" if args.apply else "仅预览", "；选择器=", provider,
              "；不输出配置值，不联网、不启动服务。")
        return 0
    except ValueError:
        print("LLM_ENV_MIGRATION：配置存在歧义或编码问题；请人工检查，未执行业务。")
    except OSError:
        print("LLM_ENV_MIGRATION：本地文件读写失败；请人工检查文件权限。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
