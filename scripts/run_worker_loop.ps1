<#
.SYNOPSIS
    로컬 worker 루프. `run_workers --once` 를 주기적으로 부른다.

.DESCRIPTION
    새 스케줄러를 만들지 않는다
    -------------------------
    저장소의 결정은 이미 나와 있다(`docs/49_worker_operations_runbook.md`):
    **스케줄링은 바깥이 하고, 중복 실행은 각 worker 의 advisory lock 이 막는다.**
    서버에서는 그 바깥이 cron / systemd timer 다. 개인 PC 에는 그게 없으니
    이 루프가 그 자리를 대신한다.

    그래서 여기서 하는 일은 "기다렸다가 기존 CLI 를 부른다" 가 전부다.
    업무 로직도, 재시도 정책도, 잠금도 여기 없다.

    PC 가 자는 동안
    -------------
    절전에서 깨어나면 이 루프는 그냥 다음 주기에 돈다. 놓친 주기를 보정하지
    않는다 — 보정할 필요가 없기 때문이다. 동기화 창이 **마지막 성공 시각**
    으로 계산되므로(order_sync 모듈 docstring), 얼마나 자주 돌았는지와 무관하게
    같은 구간을 덮는다. 놓친 실행을 세는 장치를 두면 그 장치가 또 틀린다.

    실패해도 루프는 멈추지 않는다
    -------------------------
    한 바퀴가 실패하는 것과 스케줄이 죽는 것은 다르다. 실패는 로그와
    `worker_run` 에 남고, 다음 주기가 다시 시도한다.

    종료 요청
    --------
    `var/run/worker.stop` 파일이 생기면 **이번 바퀴를 마치고** 끝낸다.
    Windows 에는 콘솔 프로세스에 보낼 마땅한 종료 신호가 없어서 파일로
    협조한다. 자는 동안에도 짧은 간격으로 확인하므로, 10분 주기여도 종료가
    10분 걸리지 않는다.

    이게 없으면 종료 스크립트가 강제 종료밖에 못 하고, 트랜잭션 한가운데서
    끊긴 실행이 RUNNING 으로 남는다.

.PARAMETER IntervalMinutes
    한 바퀴 간격. 기본 10분 — `docs/49` 의 cron 예시와 같은 값이다.

.PARAMETER MaxCycles
    이 횟수만큼 돌고 종료 (시험용). 0 이면 무한.
#>

param(
    [int]$IntervalMinutes = 0,
    [int]$MaxCycles = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

Import-Module (Join-Path $PSScriptRoot 'lib\SellerOs.psm1') -Force

$paths = Get-SellerOsPaths

if ($IntervalMinutes -le 0) {
    $fromEnv = $env:SELLEROS_WORKER_INTERVAL_MINUTES
    if ($fromEnv) { $IntervalMinutes = [int]$fromEnv } else { $IntervalMinutes = 10 }
}

$stopFlag = Get-SellerOsStopFlagPath -Component 'worker'

function Test-StopRequested { return (Test-Path $stopFlag) }

function Wait-WithStopCheck {
    <# 자면서도 종료 요청을 본다. 통으로 자면 종료가 주기만큼 걸린다. #>
    param([int]$Seconds)
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (Test-StopRequested) { return $true }
        Start-Sleep -Seconds 2
    }
    return (Test-StopRequested)
}

Write-SellerOsLog -LogName 'worker' -Level 'INFO' `
    -Message "worker 루프 시작 (간격 ${IntervalMinutes}분, pid $PID)"

$cycle = 0
while ($true) {
    if (Test-StopRequested) {
        Write-SellerOsLog -LogName 'worker' -Level 'INFO' -Message "종료 요청 — 루프를 끝냅니다"
        break
    }
    $cycle++
    Push-Location $paths.Backend
    try {
        $output = & python -m app.scripts.run_workers --once --trigger SCHEDULED 2>&1
        $code = $LASTEXITCODE
    } catch {
        $output = $_.Exception.Message
        $code = 2
    } finally {
        Pop-Location
    }

    # 종료 코드의 뜻은 CLI 가 정한다 (0 전부 성공 / 1 일부 실패 / 2 실행 불가).
    # 여기서 다시 해석하지 않는다.
    $level = switch ($code) { 0 { 'OK' } 1 { 'WARN' } default { 'FAIL' } }
    Write-SellerOsLog -LogName 'worker' -Level $level `
        -Message "worker 한 바퀴 #$cycle 종료 코드 $code"

    if ($code -ne 0) {
        # 상세는 파일에만. 화면에는 이미 한 줄이 갔다.
        $file = Join-Path $paths.LogDir ("worker-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
        ($output -join "`n") | Out-File -FilePath $file -Append -Encoding utf8
    }

    if ($MaxCycles -gt 0 -and $cycle -ge $MaxCycles) { break }
    if (Wait-WithStopCheck -Seconds ($IntervalMinutes * 60)) {
        Write-SellerOsLog -LogName 'worker' -Level 'INFO' -Message "종료 요청 — 루프를 끝냅니다"
        break
    }
}

Write-SellerOsLog -LogName 'worker' -Level 'INFO' -Message "worker 루프 종료 (총 $cycle 바퀴)"
