<# 公式验收请求账本：UTF-8 BOM兼容Windows PowerShell 5.1；保留意图并人工确认同源重试。 #>

function Write-FormulaAttemptJson {
    <# 排他创建UTF-8 JSON；不覆盖历史意图、结果或失败回执。 #>
    param([string]$Path, $Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 90))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}

function Invoke-FormulaExtractAttempt {
    <# 仅对明确许可的同来源执行一次回调；跨进程互斥，未知旧请求须人工核对终态及费用。 #>
    param(
        [Parameter(Mandatory=$true)][string]$WorkDir,
        [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$CaseId,
        [Parameter(Mandatory=$true)][hashtable]$Request,
        [Parameter(Mandatory=$true)][scriptblock]$SendRequest,
        [switch]$RetryFailedExtract
    )
    if ($Request.allow_cloud -isnot [bool] -or $Request.allow_cloud -ne $true) {
        throw 'FORMULA_CLOUD_PERMISSION'
    }
    $identityFields = @('kb_id','doc_id','chunk_id','block_id')
    foreach ($field in $identityFields) {
        $identity = [guid]::Empty
        if (-not [guid]::TryParse([string]$Request[$field], [ref]$identity) -or $identity -eq [guid]::Empty) {
            throw 'FORMULA_ATTEMPT_INPUT'
        }
    }
    $work = [IO.Path]::GetFullPath($WorkDir)
    [IO.Directory]::CreateDirectory($work) | Out-Null
    $intent = Join-Path $work "extract_attempt_$CaseId.json"
    $destination = Join-Path $work "definition_$CaseId.json"
    # 固定锁文件保留；OS句柄在退出时释放，锁本身不作为请求是否结束的证据。
    $lock = $null
    try {
        try {
            $lock = [IO.File]::Open("$intent.lock", [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        } catch { throw 'FORMULA_ATTEMPT_LOCKED' }
        if (Test-Path -LiteralPath $destination) { throw 'FORMULA_DEFINITION_EXISTS: reuse the saved result.' }
        $hasAttempt = Test-Path -LiteralPath $intent
        if ($hasAttempt -and -not $RetryFailedExtract) {
            throw 'FORMULA_RETRY_REQUIRED: inspect the previous request and use -RetryFailedExtract explicitly.'
        }
        if (-not $hasAttempt -and $RetryFailedExtract) { throw 'FORMULA_RETRY_WITHOUT_ATTEMPT' }
        if ($hasAttempt) {
            # 兼容旧版仅记录Block的意图；新记录额外冻结四个业务ID，不可换来源绕过账本。
            $history = @(Get-ChildItem -LiteralPath $work -File | Where-Object {
                $_.Name -match ('^extract_attempt_' + [regex]::Escape($CaseId) + '(?:_retry_[a-f0-9]{32})?\.json$')
            })
            foreach ($file in $history) {
                try { $previous = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { throw 'FORMULA_ATTEMPT_INVALID' }
                if ($previous.case_id -cne $CaseId -or
                    [string]$previous.candidate_block_id -ine [string]$Request.block_id) {
                    throw 'FORMULA_RETRY_SOURCE_MISMATCH'
                }
                if ($previous.request) {
                    foreach ($field in $identityFields) {
                        if ([string]$previous.request.$field -ine [string]$Request[$field]) {
                            throw 'FORMULA_RETRY_SOURCE_MISMATCH'
                        }
                    }
                }
            }
            $confirmation = Read-Host "Verify earlier $CaseId requests have ended, inspect billing, and accept ONE additional potentially paid LLM call (up to two POSTs with configured fallback). Type RETRY $CaseId"
            if ($confirmation -cne "RETRY $CaseId") { throw 'FORMULA_RETRY_NOT_CONFIRMED' }
            $intent = Join-Path $work ("extract_attempt_{0}_retry_{1}.json" -f $CaseId, [guid]::NewGuid().ToString('N'))
        }
        $frozenRequest = @{}
        foreach ($field in $identityFields) { $frozenRequest[$field] = [string]$Request[$field] }
        Write-FormulaAttemptJson $intent @{
            case_id=$CaseId; candidate_block_id=[string]$Request.block_id; request=$frozenRequest
            attempted_at_utc=[DateTime]::UtcNow.ToString('o'); potentially_billable=$true
            retry_authorized=[bool]$hasAttempt; state='REQUEST_STARTED'
        }
        $outcome = [IO.Path]::ChangeExtension($intent, 'outcome.json')
        try {
            # 回调只执行一次；网络不确定或格式失败均不自动再发请求。
            $definition = & $SendRequest $Request
            if ($definition.status -notin @('success','insufficient_evidence','unsupported_formula') -or
                [string]$definition.formula_block_id -ine [string]$Request.block_id -or
                [string]$definition.source.kb_id -ine [string]$Request.kb_id -or
                [string]$definition.source.doc_id -ine [string]$Request.doc_id -or
                [string]$definition.source.chunk_id -ine [string]$Request.chunk_id) {
                throw 'FORMULA_EXTRACT_RESULT_INVALID'
            }
            Write-FormulaAttemptJson $destination $definition
        } catch {
            try { Write-FormulaAttemptJson $outcome @{state='REQUEST_FAILED_OR_RESULT_UNSAVED'; potentially_billable=$true} }
            catch { } # 原始意图已保留；回执写盘失败也不能解除重复收费保护。
            throw 'FORMULA_EXTRACT_FAILED: no automatic retry; inspect the safe API logs and the attempt ledger.'
        }
        # 结果已保存时即使回执写盘失败，下次也会被definition文件阻断，不能重复收费。
        try { Write-FormulaAttemptJson $outcome @{state='RESULT_SAVED'; status=[string]$definition.status} }
        catch { }
        return $definition
    } finally {
        if ($null -ne $lock) { $lock.Dispose() }
    }
}
