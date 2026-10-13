<#
.SYNOPSIS
    Seller OS 상태 — 무엇이 떠 있고, 데이터가 최신인가.

.DESCRIPTION
    이 스크립트는 **아무것도 실행하지 않는다.** 읽기만 한다.

    두 가지를 따로 보여 준다
    ---------------------
    ```
    서비스   DB · 백엔드 · 프론트엔드 · worker 가 떠 있나
    데이터   주문 · 클레임 · 리포트 · 백업이 최신인가
    ```

    앱이 떠 있다고 데이터가 최신인 것은 아니다. 하나로 합치면 둘 중 하나가
    거짓말이 되고, 사용자는 낡은 숫자를 오늘 숫자로 믿는다.

    판정은 백엔드가 한다
    -----------------
    "최신인가" 를 여기서 판단하지 않는다 — `app/scripts/ops_status.py` 가
    답하고 이 스크립트는 그 답을 보여 준다. 판정이 두 곳에 있으면 언젠가
    갈라진다.

.PARAMETER Json
    JSON 으로 출력한다 (다른 도구가 읽을 때).
#>

param(
    [switch]$Json,
    [int]$BackendPort = 0,
    [int]$FrontendPort = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

Import-Module (Join-Path $PSScriptRoot 'lib\SellerOs.psm1') -Force

$paths = Get-SellerOsPaths
if ($BackendPort -le 0) {
    $BackendPort = if ($env:SELLEROS_BACKEND_PORT) { [int]$env:SELLEROS_BACKEND_PORT } else { 8000 }
}
if ($FrontendPort -le 0) {
    $FrontendPort = if ($env:SELLEROS_FRONTEND_PORT) { [int]$env:SELLEROS_FRONTEND_PORT } else { 3000 }
}

# ── 서비스 ──────────────────────────────────────────────────────────────

$db = Test-SellerOsDatabase -TimeoutSeconds 5

function Get-ComponentState {
    <#
    프로세스 기록과 포트를 함께 본다.

    포트만 보면 남의 프로그램을 우리 것으로 읽고, 기록만 보면 프로세스는
    살아 있는데 포트를 못 연 상태(예: 기동 중 오류)를 RUNNING 으로 읽는다.
    #>
    param([string]$Component, [int]$Port)
    $proc = Get-SellerOsProcess -Component $Component
    if (-not $proc) {
        if ($Port -gt 0 -and (Test-PortListening -Port $Port)) {
            return [pscustomobject]@{
                State = 'PORT_USED_BY_OTHER'; Pid = $null
                Detail = (Get-PortOwnerDescription -Port $Port)
            }
        }
        return [pscustomobject]@{ State = 'STOPPED'; Pid = $null; Detail = '' }
    }
    if ($Port -gt 0 -and -not (Test-PortListening -Port $Port)) {
        return [pscustomobject]@{ State = 'STARTING'; Pid = $proc.Pid; Detail = '포트가 아직 열리지 않았습니다' }
    }
    return [pscustomobject]@{ State = 'RUNNING'; Pid = $proc.Pid; Detail = '' }
}

$backend = Get-ComponentState -Component 'backend' -Port $BackendPort
$frontend = Get-ComponentState -Component 'frontend' -Port $FrontendPort
$worker = Get-ComponentState -Component 'worker' -Port 0

# 백엔드는 프로세스가 아니라 HTTP 로 한 번 더 본다.
$backendHealthy = $false
if ($backend.State -eq 'RUNNING') {
    $probe = Invoke-SellerOsHttpProbe -Url "http://127.0.0.1:$BackendPort/healthz" `
        -TimeoutSeconds 5 -AcceptStatus @(200)
    $backendHealthy = $probe.Ok
}

# ── 데이터 ──────────────────────────────────────────────────────────────

$data = $null
if ($db.Ok) {
    # 네이티브 stderr 를 파이프로 받지 않는다 (모듈의 Invoke-SellerOsPython 주석).
    $run = Invoke-SellerOsPython -Arguments @(
        '-m', 'app.scripts.ops_status', '--json',
        '--backup-root', (Join-Path $paths.Root 'backup'))
    try { $data = $run.Stdout | ConvertFrom-Json } catch { $data = $null }
}

# ── 출력 ────────────────────────────────────────────────────────────────

if ($Json) {
    [pscustomobject]@{
        services = [pscustomobject]@{
            database = if ($db.Ok) { 'RUNNING' } else { 'UNREACHABLE' }
            backend  = $backend.State
            backend_healthy = $backendHealthy
            frontend = $frontend.State
            worker   = $worker.State
        }
        data = $data
    } | ConvertTo-Json -Depth 8
    exit 0
}

function Get-DisplayWidth {
    <#
    한글은 터미널에서 두 칸을 차지한다. `-f "{0,-12}"` 는 **글자 수**로 세므로
    한글 라벨이 섞이면 열이 어긋나 읽기 나빠진다. 폭으로 센다.
    #>
    param([string]$Text)
    $width = 0
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        # 한글·CJK·전각 구간. 이 화면에 나오는 문자는 이 정도면 충분하다.
        if (($code -ge 0x1100 -and $code -le 0x115F) -or
            ($code -ge 0x2E80 -and $code -le 0xA4CF) -or
            ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
            ($code -ge 0xF900 -and $code -le 0xFAFF) -or
            ($code -ge 0xFF00 -and $code -le 0xFF60)) { $width += 2 }
        else { $width += 1 }
    }
    return $width
}

function Show-Line {
    param([string]$Label, [string]$Value, [string]$Tone = 'Gray', [string]$Extra = '')
    $pad = [math]::Max(1, 14 - (Get-DisplayWidth $Label))
    $text = "  $Label" + (' ' * $pad) + $Value
    if ($Extra) { $text += "  $Extra" }
    Write-Host $text -ForegroundColor $Tone
}

Write-Host ""
Write-Host "  Seller OS 상태" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  서비스" -ForegroundColor Cyan

Show-Line 'DB' $(if ($db.Ok) { 'RUNNING' } else { 'UNREACHABLE' }) `
    $(if ($db.Ok) { 'Green' } else { 'Red' })
Show-Line 'Backend' $(if ($backendHealthy) { 'RUNNING' } elseif ($backend.State -eq 'RUNNING') { 'RUNNING (건강 확인 실패)' } else { $backend.State }) `
    $(if ($backendHealthy) { 'Green' } elseif ($backend.State -eq 'STOPPED') { 'Gray' } else { 'Yellow' }) `
    $(if ($backend.Pid) { "pid $($backend.Pid)" } else { $backend.Detail })
Show-Line 'Frontend' $frontend.State `
    $(if ($frontend.State -eq 'RUNNING') { 'Green' } elseif ($frontend.State -eq 'STOPPED') { 'Gray' } else { 'Yellow' }) `
    $(if ($frontend.Pid) { "pid $($frontend.Pid)" } else { $frontend.Detail })
Show-Line 'Worker' $worker.State `
    $(if ($worker.State -eq 'RUNNING') { 'Green' } else { 'Gray' }) `
    $(if ($worker.Pid) { "pid $($worker.Pid)" } else { '' })

Write-Host ""
Write-Host "  데이터" -ForegroundColor Cyan

if (-not $data) {
    Write-Host "  확인할 수 없습니다 (DB 에 붙지 못했습니다)" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Show-Line '스키마' $data.schema.state `
    $(if ($data.schema.state -eq 'CURRENT') { 'Green' } else { 'Yellow' }) `
    ("DB {0} / 코드 {1}" -f $data.schema.db_revision, $data.schema.code_head)

foreach ($pair in @(@{ Key = 'ORDERS'; Label = '주문 동기화' }, @{ Key = 'CLAIMS'; Label = '클레임 수집' })) {
    $accounts = $data.sync.($pair.Key)
    if (-not $accounts -or $accounts.Count -eq 0) {
        Show-Line $pair.Label 'NEVER_RAN' 'Yellow'
        continue
    }
    foreach ($a in $accounts) {
        $ok = ($a.last_status -eq 'SUCCESS')
        $when = if ($a.last_success_at) { ([datetime]$a.last_success_at).ToString('MM-dd HH:mm') } else { '—' }
        Show-Line $pair.Label $(if ($ok) { 'HEALTHY' } else { $a.last_status }) `
            $(if ($ok) { 'Green' } else { 'Yellow' }) `
            ("마지막 성공 {0}  ({1})" -f $when, $a.account)
    }
}

Show-Line '일일리포트' $(if ($data.daily_report.built_today) { '생성됨' } else { '없음' }) `
    $(if ($data.daily_report.built_today) { 'Green' } else { 'Yellow' }) `
    $data.daily_report.report_date

$backupWhen = if ($data.backup.last_success_at) { ([datetime]$data.backup.last_success_at).ToString('MM-dd HH:mm') } else { '—' }
Show-Line '백업' $data.backup.state `
    $(if ($data.backup.state -eq 'CURRENT') { 'Green' } else { 'Yellow' }) `
    "마지막 $backupWhen"

Show-Line '예외' ("{0}건" -f $data.exceptions.open) `
    $(if ($data.exceptions.blocking -gt 0) { 'Red' } elseif ($data.exceptions.open -gt 0) { 'Yellow' } else { 'Green' }) `
    ("차단 {0}건" -f $data.exceptions.blocking)

Write-Host ""
if ($data.healthy -and $backendHealthy) {
    Write-Host "  정상" -ForegroundColor Green
    Write-Host ""
    exit 0
}
Write-Host "  확인 필요: $($data.degraded -join ', ')" -ForegroundColor Yellow
Write-Host ""
exit 1
