"""PowerShell请求账本的本地Mock回归；仅临时JSON，无HTTP/模型/原件或数据库访问。"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "formula_extract_attempt.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


@pytest.mark.skipif(POWERSHELL is None, reason="需要PowerShell；跳过不算请求账本已验证")
@pytest.mark.parametrize("scenario", ["first_success", "first_failure", "default_blocked", "retry_success",
    "retry_denied", "retry_failure", "wrong_source", "legacy_retry", "saved_definition", "locked",
    "retry_without_attempt", "unpermitted", "second_retry"])
def test_attempt_ledger_requires_manual_confirmation_and_preserves_history(tmp_path, scenario):
    """通过一次回调Stub验证默认阻断、同源重试/拒绝、互斥和不可覆盖历史，无真实请求。"""
    # 路径作为环境数据传递，不拼接到PowerShell代码；仅临时目录产生测试账本。
    environment = os.environ.copy()
    environment.update(FORMULA_TEST_HELPER=str(HELPER), FORMULA_TEST_WORK=str(tmp_path),
                       FORMULA_TEST_SCENARIO=scenario)
    script = r"""
$ErrorActionPreference = 'Stop'
. $env:FORMULA_TEST_HELPER
function Invoke-RestMethod {
    <# 禁止任何未被回调替换的网络调用。 #>
    throw 'UNEXPECTED_NETWORK'
}
function Read-Host {
    <# 模拟人工确认，记录次数，不读取控制台或执行收费请求。 #>
    param($Prompt)
    $script:prompts++
    if ($env:FORMULA_TEST_SCENARIO -eq 'retry_denied') { return 'NO' }
    return 'RETRY F01'
}
$script:calls = 0; $script:prompts = 0
$request = @{kb_id='11111111-1111-1111-1111-111111111111';doc_id='22222222-2222-2222-2222-222222222222';
    chunk_id='33333333-3333-3333-3333-333333333333';block_id='44444444-4444-4444-4444-444444444444';allow_cloud=$true}
$work = $env:FORMULA_TEST_WORK
$intent = Join-Path $work 'extract_attempt_F01.json'
$dest = Join-Path $work 'definition_F01.json'
$scenario = $env:FORMULA_TEST_SCENARIO
$prior = $scenario -notin @('first_success','first_failure','retry_without_attempt','unpermitted')
if ($prior) {
    $old = @{case_id='F01';candidate_block_id=$request.block_id}
    if ($scenario -ne 'legacy_retry') {
        $old.request = @{kb_id=$request.kb_id;doc_id=$request.doc_id;chunk_id=$request.chunk_id;block_id=$request.block_id}
    }
    Write-FormulaAttemptJson $intent $old
    $before = [IO.File]::ReadAllText($intent)
}
if ($scenario -eq 'wrong_source') { $request.chunk_id = '55555555-5555-5555-5555-555555555555' }
if ($scenario -eq 'unpermitted') { $request.allow_cloud = $false }
if ($scenario -eq 'saved_definition') { Write-FormulaAttemptJson $dest @{status='success';sentinel='KEEP'} }
$lock = $null
if ($scenario -eq 'locked') {
    $lock = [IO.File]::Open("$intent.lock",[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
}
$retry = $scenario -in @('retry_success','retry_denied','retry_failure','wrong_source','legacy_retry',
    'saved_definition','retry_without_attempt','second_retry')
$send = {
    param($body)
    $script:calls++
    if ($scenario -in @('first_failure','retry_failure','second_retry')) { throw 'PRIVATE_UPSTREAM_BODY' }
    return [pscustomobject]@{status='success';formula_block_id=$body.block_id;
        source=[pscustomobject]@{kb_id=$body.kb_id;doc_id=$body.doc_id;chunk_id=$body.chunk_id}}
}
$errorCode = ''
try {
    $null = Invoke-FormulaExtractAttempt -WorkDir $work -CaseId F01 -Request $request -RetryFailedExtract:$retry -SendRequest $send
} catch { $errorCode = $_.Exception.Message.Split(':')[0] }
finally { if ($null -ne $lock) { $lock.Dispose() } }
if ($scenario -eq 'second_retry') {
    # 第一次重试仍失败后，默认调用必须继续阻断；再一次人工确认才可追加新意图。
    try { $null = Invoke-FormulaExtractAttempt -WorkDir $work -CaseId F01 -Request $request -SendRequest $send }
    catch { if ($_.Exception.Message -notlike 'FORMULA_RETRY_REQUIRED*') { throw 'DEFAULT_NOT_BLOCKED' } }
    try { $null = Invoke-FormulaExtractAttempt -WorkDir $work -CaseId F01 -Request $request -RetryFailedExtract -SendRequest $send }
    catch { if ($_.Exception.Message -notlike 'FORMULA_EXTRACT_FAILED*') { throw 'WRONG_FAILURE' } }
}
$history = @(Get-ChildItem -LiteralPath $work -Filter '*.json' | Where-Object {
    $_.Name -match '^extract_attempt_F01(?:_retry_[a-f0-9]{32})?\.json$'
})
$outcomes = @(Get-ChildItem -LiteralPath $work -Filter '*.outcome.json')
$preserved = (-not $prior) -or ([IO.File]::ReadAllText($intent) -ceq $before)
$sentinel = $null
if ($scenario -eq 'saved_definition') { $sentinel = (Get-Content -LiteralPath $dest -Raw | ConvertFrom-Json).sentinel }
@{calls=$script:calls;prompts=$script:prompts;error_code=$errorCode;history_count=$history.Count;
    outcome_count=$outcomes.Count;preserved=$preserved;has_definition=(Test-Path -LiteralPath $dest);
    sentinel=$sentinel} | ConvertTo-Json -Compress
"""
    completed = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment, text=True, encoding="utf-8", capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert "PRIVATE" not in completed.stdout + completed.stderr
    expected = {
        "first_success": (1, 0, "", 1, True),
        "first_failure": (1, 0, "FORMULA_EXTRACT_FAILED", 1, False),
        "default_blocked": (0, 0, "FORMULA_RETRY_REQUIRED", 1, False),
        "retry_success": (1, 1, "", 2, True),
        "retry_denied": (0, 1, "FORMULA_RETRY_NOT_CONFIRMED", 1, False),
        "retry_failure": (1, 1, "FORMULA_EXTRACT_FAILED", 2, False),
        "wrong_source": (0, 0, "FORMULA_RETRY_SOURCE_MISMATCH", 1, False),
        "legacy_retry": (1, 1, "", 2, True),
        "saved_definition": (0, 0, "FORMULA_DEFINITION_EXISTS", 1, True),
        "locked": (0, 0, "FORMULA_ATTEMPT_LOCKED", 1, False),
        "retry_without_attempt": (0, 0, "FORMULA_RETRY_WITHOUT_ATTEMPT", 0, False),
        "unpermitted": (0, 0, "FORMULA_CLOUD_PERMISSION", 0, False),
        "second_retry": (2, 2, "FORMULA_EXTRACT_FAILED", 3, False),
    }[scenario]
    assert (result["calls"], result["prompts"], result["error_code"], result["history_count"],
            result["has_definition"]) == expected
    assert result["preserved"] is True
    assert result["outcome_count"] == result["calls"]
    if scenario == "saved_definition":
        assert result["sentinel"] == "KEEP"
