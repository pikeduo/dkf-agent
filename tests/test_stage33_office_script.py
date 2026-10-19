"""执行恢复脚本的隔离控制流测试；HTTP全由PowerShell内存函数替代，报告只写临时目录。"""

import csv
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

# harness覆盖所有HTTP入口；未知路径直接失败，绝不回落到真实Invoke-RestMethod。
HARNESS = r'''
param([string]$RecoveryScript, [string]$Scenario, [string]$Mode)
$ErrorActionPreference = 'Stop'
# 独立测试进程中的专有上下文，避免被被测脚本的子作用域遮蔽。
$global:stage33RecoveryTest = @{
    posts = [System.Collections.Generic.List[string]]::new()
    first = '4d18710e-c8bf-4504-8982-bd5ce7e115a9'
    second = 'e1f59a27-a401-4de1-809b-a998ae7c63f1'
    kb = '158030a1-42d6-42ee-835d-02fdbff23f81'
}
function Start-Sleep {
    # 测试只推进模拟状态，不实际等待。
    param([int]$Seconds)
}
function Invoke-RestMethod {
    # 所有网络均隔离；保留真实脚本的分页/等待/失败分类及投递决策。
    param([string]$Uri, [string]$Method = 'Get', [int]$TimeoutSec)
    if ($Uri -match '/knowledge-bases/.+/documents\?') {
        $all = @(@{ doc_id=$global:stage33RecoveryTest.first; file_name='metadata.docx' },
                 @{ doc_id=$global:stage33RecoveryTest.second; file_name='metadata.pptx' })
        1..198 | ForEach-Object { $all += @{ doc_id="ready-$_"; file_name='metadata' } }
        return @{ items=$all; total=200 }
    }
    if ($Uri -notmatch '/documents/([^/?]+)(/status|/retry)?$') { throw 'UNEXPECTED_HTTP' }
    $id, $suffix = $Matches[1], $Matches[2]
    if ($Method -eq 'Post') {
        if ($suffix -ne '/retry') { throw 'UNEXPECTED_WRITE' }
        $global:stage33RecoveryTest.posts.Add($id)
        return @{}
    }
    if ($suffix -eq '/status') {
        if ($id -eq $global:stage33RecoveryTest.first -and $global:stage33RecoveryTest.posts.Contains($id) -and $Scenario -eq 'http_failure') {
            throw 'SIMULATED_HTTP_UNCONFIRMED'
        }
        $failed = $id -in @($global:stage33RecoveryTest.first, $global:stage33RecoveryTest.second) -and
                  (-not $global:stage33RecoveryTest.posts.Contains($id) -or $id -eq $global:stage33RecoveryTest.first)
        return [pscustomobject]@{ doc_id=$id; kb_id=$global:stage33RecoveryTest.kb
            status=$(if ($failed) { 'FAILED' } else { 'READY' })
            block_count=$(if ($failed) { 0 } else { 1 }); chunk_count=$(if ($failed) { 0 } else { 1 })
            vector_count=$(if ($failed) { 0 } else { 1 }); invalid_vector_count=0
            management=@{ state=$(if ($failed) { 'failed' } else { 'done' }); stage='parse'
                          cause_code='EXTERNAL_ATTACHMENT_MISSING' }
            error_message='EXTERNAL_ATTACHMENT_MISSING' }
    }
    if (-not $suffix) {
        $existing = @()
        if ($Scenario -eq 'existing_job') { $existing = @(@{job_id='old'}) }
        return @{ doc_id=$id; kb_id=$global:stage33RecoveryTest.kb
                  parse_jobs=$existing }
    }
    throw 'UNEXPECTED_HTTP'
}
$options = @{}
if ($Mode -ne 'read_only') { $options.ExecuteSavedOffice=$true; $options.AllowCloud=$true }
if ($Mode -eq 'continue') { $options.ContinueSavedOfficeOnFailure=$true }
if ($Mode -eq 'invalid') { $options=@{ContinueSavedOfficeOnFailure=$true} }
$blocked = $false
try { & $RecoveryScript @options | Out-Null } catch { $blocked = $true }
@{ posts=@($global:stage33RecoveryTest.posts.ToArray()); blocked=$blocked } | ConvertTo-Json -Compress
'''


@pytest.mark.parametrize("mode,scenario,count,blocked", [
    ("read_only", "terminal_failure", 0, False),
    ("default", "terminal_failure", 1, False),
    ("continue", "terminal_failure", 2, False),
    ("continue", "http_failure", 1, True),
    ("continue", "existing_job", 0, True),
    ("invalid", "terminal_failure", 0, True),
])
def test_saved_office_continuation_requires_explicit_mode_and_confirmed_failure(tmp_path, mode, scenario, count, blocked):
    """明确失败可继续下一份，默认停组；已有Job、未确认HTTP和非法开关不能被继续选项绕过。"""
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not shell:
        pytest.skip("需要PowerShell执行隔离脚本控制流测试；未验证不能算通过")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    recovery = scripts / "recover_stage33_office.ps1"
    recovery.write_text((ROOT / "scripts/recover_stage33_office.ps1").read_text(encoding="utf-8-sig"),
                        encoding="utf-8-sig")
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8-sig")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(harness),
                             "-RecoveryScript", str(recovery), "-Scenario", scenario, "-Mode", mode],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(summary["posts"]) == count and summary["blocked"] is blocked
    assert len(set(summary["posts"])) == count  # 每份最多POST一次。
    if not blocked:
        reports = list((tmp_path / "results/stage33").glob("recovery_status_*.csv"))
        assert len(reports) == 1
        with reports[0].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 200
        failed = next(row for row in rows if row["doc_id"].startswith("4d18710e"))
        assert failed["status"] == "FAILED" and failed["passed"] == "False"
