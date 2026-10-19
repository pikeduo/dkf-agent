<# 阶段33原知识库全量状态检查与分组恢复；默认只查询，写操作必须由用户显式选择。 #>
[CmdletBinding()]
param(
    [switch]$ExecuteLocal,
    [switch]$ExecuteNewParse,
    [switch]$ExecuteSavedOffice,
    [switch]$ContinueSavedOfficeOnFailure,
    [switch]$AllowCloud,
    [ValidateRange(60,86400)][int]$TimeoutSeconds = 7200
)

$ErrorActionPreference = 'Stop'
$executionModes = @($ExecuteLocal, $ExecuteNewParse, $ExecuteSavedOffice) | Where-Object { $_ }
if (@($executionModes).Count -gt 1) { throw '请分别执行本地恢复、新解析和Office保存快照恢复。' }
if ($ContinueSavedOfficeOnFailure -and -not $ExecuteSavedOffice) {
    throw '-ContinueSavedOfficeOnFailure仅用于显式Office保存快照恢复。'
}
if (($ExecuteNewParse -or $ExecuteSavedOffice) -and -not $AllowCloud) {
    throw '首次云解析可能收费；确认费用后须显式增加 -AllowCloud。'
}
if ($AllowCloud -and -not ($ExecuteNewParse -or $ExecuteSavedOffice)) {
    throw '-AllowCloud仅用于显式新解析或Office保存快照恢复。'
}
$apiRoot = 'http://127.0.0.1:8000/api/admin'
$kbId = '158030a1-42d6-42ee-835d-02fdbff23f81'
# 仅已检查过的缓存恢复项；不会从通用错误码推断其他文件也可重建。
$localIds = @(
    'b37f4679-2701-4d54-8165-c80fd0840611', '8b72d308-ac54-4506-bf8d-734cacaf1b28',
    '897d54a0-dd38-42ad-b7e5-d0a22b35f956', 'e4c88d4d-f816-4b55-a0ea-92a5d642ec16',
    '3649c30e-d0cd-4c36-95cc-4842599156e5', 'ce7e99cf-0928-4609-a1bf-2baa6781bc86',
    'be585058-60d3-4e7a-a96b-db0bc23f6d28',
    'be97464e-7ecc-4fd3-9af7-14a830c3bdf6', '5bf1bd8a-8879-4472-a2c7-4ff04bcba0ef',
    'd517319d-c348-49c6-925e-4abf894d1b8d'
)
$newParseIds = @(
    'da3d2a31-4be8-454a-82a2-e5195b2c2da8', '6978b773-c412-4541-96ea-07ebf3517c90',
    '68989d39-3032-40c0-8d42-dddb29ea2185', '3f73d303-099f-4698-8910-4748667f64d2',
    '7440559b-26e2-40dd-ae35-39881536ac11', 'ee5e1f7a-0208-4c2d-adbc-eca7b98a4aa7',
    '7339e772-0627-40bf-b90f-a0e6646c0356', '83d24856-fb63-49f9-9466-8db2bf8543c8'
)
# 仅本轮逐项核过关系元数据的九份；没有云Job时才允许首次提交，绝不Force或补投旧Batch。
$savedOfficeIds = @(
    '4d18710e-c8bf-4504-8982-bd5ce7e115a9', '926d4339-96f6-4756-af61-121901efd806',
    'e1f59a27-a401-4de1-809b-a998ae7c63f1', 'c8e4ca42-f739-48c4-9c2d-50120f956309',
    '2d4cb398-1f04-461a-87e8-84437fcf2554', 'fd0eed46-d01c-450d-901e-d6c10edcdb68',
    'd5eefa49-4d03-4e66-aa33-54b7d27e5734', '5f7494bb-d83a-41cf-b4ee-30a618b4da60',
    'b353a134-04d8-4cb2-ae30-9d87301f459f'
)
# 已确认切片成功、聚合预算检查阻止GPU任务创建的两份；恢复只接续现有Chunk。
$embeddingBudgetIds = @(
    'be585058-60d3-4e7a-a96b-db0bc23f6d28', '7339e772-0627-40bf-b90f-a0e6646c0356'
)

function Get-Stage33Status {
    # 只查询原知识库文档；关联不符时停止，不把其他库或旧checkpoint当作当前结果。
    param([string]$DocId)
    $state = Invoke-RestMethod "$apiRoot/documents/$DocId/status" -TimeoutSec 30
    if ($state.kb_id -ne $kbId) { throw "知识库身份不符：$DocId" }
    return $state
}

function Wait-Stage33Recovery {
    # 每份只POST一次，后续有界查询；超时/HTTP异常不盲重发，不自动继续下一份。
    param([string]$DocId)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 5
        $state = Get-Stage33Status $DocId
        if ($state.status -in @('READY','PARTIAL_READY') -or $state.management.state -eq 'failed') {
            return $state
        }
    } while ((Get-Date) -lt $deadline)
    throw "等待超时：$DocId；后台可能仍在处理，请先GET状态，勿重复POST。"
}

function ConvertTo-Stage33Row {
    # 同时展示业务/管理/质量；保存快照单独授权首次云任务，不冒充完整READY。
    param($Document, $State)
    $passed = ($State.status -eq 'READY' -and $State.block_count -gt 0 -and
        $State.chunk_count -gt 0 -and $State.chunk_count -eq $State.vector_count -and
        $State.invalid_vector_count -eq 0)
    $action = 'WAIT_OR_REVIEW'
    if ($passed) { $action = 'COMPLETE' }
    elseif ($State.status -eq 'PARTIAL_READY') { $action = 'QUALITY_REVIEW' }
    elseif ($Document.doc_id -in $embeddingBudgetIds -and $State.status -eq 'EMBEDDING' -and
            -not $State.embedding_task_id -and $State.management.state -eq 'failed' -and
            $State.management.cause_code -eq 'EMBEDDING_LIMIT') { $action = 'LOCAL_RETRY' }
    elseif ($Document.doc_id -eq 'efdf30a0-e412-4d28-8a3a-5257f2fe62a0' -and
            $State.status -eq 'FAILED' -and $State.management.cause_code -eq 'EMBEDDING_TOO_LONG') {
        # v4遗留12238真实Token长片；v5须单文档显式验收，不在全量脚本中盲重建。
        $action = 'TOKEN_SOURCE_REVIEW'
    }
    elseif ($State.status -eq 'FAILED') {
        if ($Document.doc_id -in $localIds) {
            $action = if ($State.parse_cache_present) { 'LOCAL_REINDEX' } else { 'LOCAL_CACHE_REQUIRED' }
        } elseif ($Document.doc_id -in $newParseIds) { $action = 'NEW_PARSE_REQUIRES_CLOUD_PERMISSION' }
        elseif ($Document.doc_id -in $savedOfficeIds -and (
            $State.management.cause_code -in @('EXTERNAL_ATTACHMENT_MISSING','XLSX_EXTERNAL_ASSET_MISSING') -or
            $State.error_message -match 'EXTERNAL_ATTACHMENT_MISSING|XLSX_EXTERNAL_ASSET_MISSING')) {
            $action = 'SAVED_OFFICE_REQUIRES_CLOUD_PERMISSION'
        }
        elseif ($State.management.cause_code -eq 'EXTERNAL_ATTACHMENT_MISSING') { $action = 'EXTERNAL_CONTENT_REQUIRED' }
        elseif ($State.management.cause_code -eq 'PREFLIGHT_LIMIT') { $action = 'XML_BUDGET_REVIEW' }
        elseif ($State.management.stage -eq 'cloud') { $action = 'ORIGINAL_CLOUD_JOB_REVIEW' }
    }
    elseif ($State.management.state -eq 'failed') { $action = 'MANAGEMENT_FAILED_REVIEW' }
    return [pscustomobject]@{
        doc_id = $Document.doc_id; file = $Document.file_name; status = $State.status
        stage = $State.management.stage; blocks = $State.block_count; chunks = $State.chunk_count
        vectors = $State.vector_count; invalid_vectors = $State.invalid_vector_count
        passed = $passed; action = $action; cause = $State.management.cause_code
        management_state = $State.management.state; management_error = $State.management.error_code
        error = $State.error_message; quality = $State.parse_quality.content
        warnings = ($State.parse_quality.warnings -join ',')
        missing_pages = ($State.parse_quality.missing_pages -join ',')
        visual_review_pages = ($State.parse_quality.visual_review_pages -join ',')
        diagnostic_reasons = (($State.parse_quality.diagnostics | ForEach-Object { $_.reason }) -join ',')
    }
}

$documents = @()
$offset = 0
do {
    $page = Invoke-RestMethod "$apiRoot/knowledge-bases/$kbId/documents?limit=100&offset=$offset" -TimeoutSec 30
    $documents += @($page.items)
    $offset += @($page.items).Count
} while ($offset -lt $page.total -and @($page.items).Count -gt 0)
if ($documents.Count -ne 200 -or @($documents.doc_id | Select-Object -Unique).Count -ne 200) {
    throw "原知识库文档数或身份不符，实取$($documents.Count)份，预期200份；未执行恢复。"
}

$savedOfficeStopped = $false
$rows = @(
    foreach ($document in $documents) {
        $state = Get-Stage33Status $document.doc_id
        $row = ConvertTo-Stage33Row $document $state
        $operation = $null
        if ($ExecuteLocal -and $row.action -eq 'LOCAL_REINDEX') { $operation = 'reindex' }
        if ($ExecuteLocal -and $row.action -eq 'LOCAL_RETRY') { $operation = 'retry' }
        if (($ExecuteNewParse -and $row.action -eq 'NEW_PARSE_REQUIRES_CLOUD_PERMISSION') -or
            ($ExecuteSavedOffice -and -not $savedOfficeStopped -and
             $row.action -eq 'SAVED_OFFICE_REQUIRES_CLOUD_PERMISSION')) {
            $detail = Invoke-RestMethod "$apiRoot/documents/$($document.doc_id)" -TimeoutSec 30
            if ($detail.doc_id -ne $document.doc_id -or $detail.kb_id -ne $kbId) {
                throw '文档详情身份不符，未提交新解析。'
            }
            if (@($detail.parse_jobs).Count -gt 0) {
                throw "已有Job：$($document.doc_id)，停止新解析，请先核对原结果和费用。"
            }
            $operation = 'retry'
        }
        if ($operation -and $state.management.state -ne 'running') {
            Write-Host "串行恢复：$($document.doc_id) / $operation"
            Invoke-RestMethod -Method Post "$apiRoot/documents/$($document.doc_id)/$operation" -TimeoutSec 120 | Out-Null
            $state = Wait-Stage33Recovery $document.doc_id
            # 解析/切片失败可能保留旧Chunk；只在已推进向量阶段或成功时判断实际新切片版本。
            if ($operation -eq 'reindex' -and $state.chunk_count -gt 0 -and
                ($state.status -in @('READY','PARTIAL_READY') -or $state.management.stage -in @('embed','index'))) {
                $sample = Invoke-RestMethod "$apiRoot/documents/$($document.doc_id)/chunks?limit=1&offset=0" -TimeoutSec 30
                if ($sample.items[0].metadata.chunker_version -ne 'block-chunk-v5') {
                    throw '仍由旧Worker生成切片，停止后续恢复；先安全重启所有消费default_queue的旧Worker。'
                }
            }
            $row = ConvertTo-Stage33Row $document $state
            Write-Host "处理结果：$($document.doc_id) / $($row.status) / $($row.cause)"
            if ($ExecuteSavedOffice -and $state.status -notin @('READY','PARTIAL_READY') -and
                -not ($ContinueSavedOfficeOnFailure -and $state.status -eq 'FAILED' -and
                      $state.management.state -eq 'failed')) {
                # 显式许可仅允许跨过明确失败项；未确认请求、超时和在途任务仍停止，绝不重投已有Job。
                $savedOfficeStopped = $true
                Write-Warning 'Office恢复未完成，已停止该组后续新提交；请先检查此文档原始阶段和原因。'
            }
        }
        $row
    }
)

$reportDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) 'results/stage33'
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
$report = Join-Path $reportDirectory ("recovery_status_{0}.csv" -f (Get-Date -Format 'yyyyMMdd_HHmmssfff'))
$rows | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding UTF8
$rows | Group-Object status | Sort-Object Name | Format-Table Name,Count -AutoSize
$rows | Where-Object { -not $_.passed } | Format-Table doc_id,status,stage,management_state,cause,action -AutoSize
Write-Host "完整入库检查通过：$(@($rows | Where-Object { $_.passed }).Count)/200；逐项报告：$report"
