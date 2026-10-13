<#
.SYNOPSIS
    Windows 로그인 시 Seller OS 자동 시작 (선택).

.DESCRIPTION
    **기본으로 등록하지 않는다.** PC 를 켰다고 늘 Seller OS 를 쓰는 것은
    아니다. 매번 백엔드·프론트·worker 가 뜨고 벤더 API 를 부르는 것을
    사용자가 원하지 않을 수 있고, 원하지 않는 자동 실행은 신뢰를 깎는다.

    쓰고 싶은 사람만 이 스크립트를 한 번 실행한다.

    무엇이 등록되나
    -------------
    Windows 작업 스케줄러에 로그인 트리거 작업 하나. 지연 실행을 두는 이유는
    로그인 직후에는 네트워크와 PostgreSQL 서비스가 아직 준비되지 않을 수
    있기 때문이다 — 그때 시작하면 DB 접속 실패로 끝난다.

    브라우저는 열지 않는다
    -------------------
    자동 시작은 "쓰려고 켠 것" 이 아니다. 로그인할 때마다 창이 뜨면 방해가
    된다. 서비스와 따라잡기만 하고, 화면은 사용자가 열 때 연다.

.PARAMETER Remove
    등록된 작업을 지운다.

.PARAMETER DelayMinutes
    로그인 후 지연 시간. 기본 2분.
#>

param(
    [switch]$Remove,
    [int]$DelayMinutes = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'SellerOS Autostart'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$start = Join-Path $root 'scripts\start_seller_os.ps1'

function Test-TaskExists {
    return $null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
}

if ($Remove) {
    if (Test-TaskExists) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  자동 시작을 해제했습니다." -ForegroundColor Green
    } else {
        Write-Host "  등록된 자동 시작이 없습니다." -ForegroundColor Gray
    }
    Write-Host ""
    exit 0
}

if (-not (Test-Path $start)) { throw "시작 스크립트를 찾지 못했습니다: $start" }

# 브라우저를 열지 않고, 실패해도 창을 붙들지 않는다 (무인 실행이므로).
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$start`" -SkipBrowser" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelayMinutes}M"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'PICKSOME Seller OS 로컬 운영 자동 시작' `
        -Force | Out-Null
} catch {
    Write-Host "  작업을 등록하지 못했습니다." -ForegroundColor Red
    Write-Host "  → 관리자 권한 PowerShell 에서 다시 실행해 보세요." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  자동 시작을 등록했습니다." -ForegroundColor Green
Write-Host "  로그인 $DelayMinutes 분 뒤 서비스가 뜨고 따라잡기가 돕니다 (브라우저는 열지 않습니다)." -ForegroundColor Gray
Write-Host "  해제: scripts\install_autostart.ps1 -Remove" -ForegroundColor Gray
Write-Host ""
