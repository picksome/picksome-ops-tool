<#
.SYNOPSIS
    Seller OS 종료.

.DESCRIPTION
    순서에 의미가 있다
    ----------------
    ```
    1. worker      먼저. 트랜잭션이 끝날 시간을 준다
    2. 백엔드      worker 가 쓰던 연결이 정리된 뒤
    3. 프론트엔드
    4. 데이터베이스 기본적으로 건드리지 않는다
    ```

    worker 를 먼저 세우는 이유는 그것만 **쓰기 작업 중일 수 있기** 때문이다.
    백엔드를 먼저 내리면 worker 가 붙어 있던 연결이 끊기면서 실행 기록이
    RUNNING 으로 남는다. 그 기록은 다음 기동이 정리하지만, 정리할 일을
    만들지 않는 편이 낫다.

    DB 는 왜 안 끄는가
    ----------------
    이 PC 의 PostgreSQL 은 Seller OS 전용이라는 보장이 없다. 서비스로 등록돼
    있고 다른 프로그램이 붙어 있을 수 있다. **끄는 것은 되돌리기 쉽지만,
    남의 작업을 끊는 것은 아니다.** 정말 전용이라면 `-StopDatabase` 를 주면 된다.

    무엇을 하지 않나
    --------------
    ```
    기록에 없는 PID 를 죽이는 일          안 한다
    포트를 쓰는 프로세스를 무조건 죽이는 일  안 한다
    데이터 정리 · 임시파일 삭제            안 한다
    ```

.PARAMETER StopDatabase
    PostgreSQL 서비스도 내린다. 이 DB 를 Seller OS 만 쓸 때에만 쓰세요.

.PARAMETER TimeoutSeconds
    정상 종료를 기다리는 시간. 지나면 강제 종료한다.
#>

param(
    [switch]$StopDatabase,
    [int]$TimeoutSeconds = 20
)

Set-StrictMode -Version Latest
# **여기서는 'Stop' 을 쓰지 않는다.** 종료 절차는 끝까지 가야 한다 — 한
# 구성요소가 안 죽는다고 나머지를 남겨 두면 다음 시작이 포트 충돌로 막힌다.
# (실측: taskkill 의 stderr 가 ErrorRecord 로 감싸져 첫 줄에서 스크립트가 죽었다.)
$ErrorActionPreference = 'Continue'

Import-Module (Join-Path $PSScriptRoot 'lib\SellerOs.psm1') -Force

Initialize-SellerOsDirs

Write-Host ""
Write-Host "  Seller OS 종료" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-SellerOsLog -LogName 'shutdown' -Level 'INFO' -Message "종료 요청 (StopDatabase=$StopDatabase)"

$labels = [ordered]@{
    worker   = 'worker'
    backend  = '백엔드'
    frontend = '프론트엔드'
}

$anyStuck = $false
foreach ($component in $labels.Keys) {
    $label = $labels[$component]
    # worker 만 협조 종료를 기다린다. 그것만 쓰기 작업 중일 수 있다.
    # 백엔드·프론트는 끊겨도 PostgreSQL 이 트랜잭션을 되돌린다 (모듈 주석).
    $isWorker = ($component -eq 'worker')
    $timeout = if ($isWorker) { [math]::Max($TimeoutSeconds, 30) } else { $TimeoutSeconds }
    $result = Stop-SellerOsProcess -Component $component -TimeoutSeconds $timeout `
        -UseStopFlag:$isWorker

    switch ($result) {
        'NOT_RUNNING' { Write-SellerOsLog -LogName 'shutdown' -Level 'INFO' -Message "$label  실행 중이 아닙니다" }
        'GRACEFUL'    { Write-SellerOsLog -LogName 'shutdown' -Level 'OK'   -Message "$label  이번 작업을 마치고 종료" }
        'STOPPED'     { Write-SellerOsLog -LogName 'shutdown' -Level 'OK'   -Message "$label  종료" }
        'KILLED' {
            # worker 가 여기 오면 협조 종료가 안 된 것이다 — 알려야 한다.
            # 나머지는 원래 이 경로로 끝난다.
            $level = if ($isWorker) { 'WARN' } else { 'OK' }
            $note = if ($isWorker) { '작업 중이라 강제 종료했습니다 (다음 시작이 실행 기록을 정리합니다)' } else { '종료' }
            Write-SellerOsLog -LogName 'shutdown' -Level $level -Message "$label  $note"
        }
        'STILL_RUNNING' {
            $anyStuck = $true
            Write-SellerOsLog -LogName 'shutdown' -Level 'FAIL' -Message "$label  종료하지 못했습니다"
        }
    }
}

# 중단된 실행 기록을 정리한다. 강제 종료했다면 RUNNING 이 남아 있을 수 있고,
# 놔두면 다음에 상태 화면이 "지금 조회 중" 이라고 말한다.
$reclaim = Invoke-SellerOsPython -Arguments @('-m', 'app.scripts.run_workers', '--reclaim-only')
if ($reclaim.ExitCode -eq 0) {
    $line = ($reclaim.Stdout -split "`r?`n" | Where-Object { $_ -match '^\{' } | Select-Object -Last 1)
    if ($line) { Write-SellerOsLog -LogName 'shutdown' -Level 'INFO' -Message "실행 기록 정리 $line" }
} else {
    Write-SellerOsLog -LogName 'shutdown' -Level 'WARN' `
        -Message "실행 기록을 정리하지 못했습니다 (DB 가 이미 내려갔을 수 있습니다)"
}

if ($StopDatabase) {
    Write-SellerOsLog -LogName 'shutdown' -Level 'INFO' -Message "PostgreSQL 서비스를 내립니다"
    try {
        Stop-Service -Name 'postgresql-16' -ErrorAction Stop
        Write-SellerOsLog -LogName 'shutdown' -Level 'OK' -Message "데이터베이스  정지"
    } catch {
        Write-SellerOsLog -LogName 'shutdown' -Level 'WARN' `
            -Message "PostgreSQL 을 내리지 못했습니다 (관리자 권한이 필요할 수 있습니다)"
    }
} else {
    Write-SellerOsLog -LogName 'shutdown' -Level 'INFO' `
        -Message "데이터베이스  그대로 둡니다 (내리려면 -StopDatabase)"
}

Write-Host ""
if ($anyStuck) {
    Write-Host "  일부 프로세스가 남아 있습니다. 작업 관리자에서 확인하세요." -ForegroundColor Yellow
    exit 1
}
Write-Host "  종료 완료" -ForegroundColor Green
Write-Host ""
exit 0
