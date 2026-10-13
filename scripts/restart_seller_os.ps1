<#
.SYNOPSIS
    Seller OS 재시작 — 종료한 뒤 시작한다.

.DESCRIPTION
    새 로직을 만들지 않는다. `stop_seller_os.ps1` 과 `start_seller_os.ps1` 을
    순서대로 부를 뿐이다. 재시작 전용 처리를 따로 두면 그 경로만 검증되지
    않은 채 남는다.

    DB 는 건드리지 않는다 — 재시작에서 DB 를 내렸다 올릴 이유가 없고,
    그 사이 다른 프로그램의 연결이 끊긴다.

    종료에 실패해도 시작을 시도한다. 남은 프로세스가 있으면 시작 쪽의
    포트 점검이 그것을 사실대로 보고한다 — 조용히 두 번째를 띄우지 않는다.
#>

param(
    [switch]$SkipCatchup,
    [switch]$SkipBrowser,
    [switch]$Rebuild
)

Set-StrictMode -Version Latest

$stop = Join-Path $PSScriptRoot 'stop_seller_os.ps1'
$start = Join-Path $PSScriptRoot 'start_seller_os.ps1'

& $stop
$stopCode = $LASTEXITCODE
if ($stopCode -ne 0) {
    Write-Host "  종료가 깔끔하지 않았습니다. 그래도 시작을 시도합니다." -ForegroundColor Yellow
}

# 포트가 완전히 풀릴 시간을 준다. TIME_WAIT 은 즉시 사라지지 않는다.
Start-Sleep -Seconds 2

# `$args` 는 PowerShell 예약 변수다. 여기에 대입하면 StrictMode 에서 터진다.
$startArgs = @{}
if ($SkipCatchup) { $startArgs['SkipCatchup'] = $true }
if ($SkipBrowser) { $startArgs['SkipBrowser'] = $true }
if ($Rebuild) { $startArgs['Rebuild'] = $true }

& $start @startArgs
exit $LASTEXITCODE
