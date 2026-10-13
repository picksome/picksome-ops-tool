<#
.SYNOPSIS
    로컬 운영 스크립트 검사. 의존성 없이 돈다.

.DESCRIPTION
    왜 Pester 를 쓰지 않나
    -------------------
    이 PC 에 있는 것은 Pester 3.4 (Windows 기본 탑재)뿐이고, 그 문법은 지금
    쓰이는 Pester 5 와 다르다. 검사 8개를 위해 테스트 프레임워크 버전을
    저장소 요구사항에 추가하지 않는다.

    무엇을 검사하나
    -------------
    셸에서만 판정할 수 있는 것들이다. 동기화 창·따라잡기·리포트 중복 같은
    업무 판단은 파이썬 테스트가 본다
    (`backend/app/tests/integration/test_startup_catchup.py`).

    ```
    프로세스 기록이 우리 것인지 판정하는가   PID 재사용을 잡는가
    죽은 프로세스를 살아 있다고 하지 않는가
    남의 프로그램이 쓰는 포트를 우리 것이라 하지 않는가
    worker 가 종료 표시를 따르는가
    공백이 든 인자를 파이썬에 온전히 넘기는가
    보존 기간이 지난 로그를 지우는가 (.log.err 포함)
    비밀값이 점검 출력에 섞이지 않는가
    ```

    **실행 중인 Seller OS 를 건드리지 않는다.** 기록 파일은 검사 전용
    이름(`__test_*`)을 쓰고, 끝나면 지운다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test\Test-LocalOperations.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

Import-Module (Join-Path $PSScriptRoot '..\lib\SellerOs.psm1') -Force

$script:Passed = 0
$script:Failed = 0

function Assert-That {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][bool]$Condition,
        [string]$Detail = ''
    )
    if ($Condition) {
        $script:Passed++
        Write-Host "  PASS  $Name" -ForegroundColor Green
    } else {
        $script:Failed++
        Write-Host "  FAIL  $Name" -ForegroundColor Red
        if ($Detail) { Write-Host "        $Detail" -ForegroundColor Red }
    }
}

$paths = Get-SellerOsPaths
Initialize-SellerOsDirs

Write-Host ""
Write-Host "  로컬 운영 스크립트 검사" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. 프로세스 기록 왕복 ───────────────────────────────────────────────
# 우리가 띄운 프로세스를 우리 것으로 알아봐야 한다. 이게 안 되면 시작
# 스크립트가 매번 두 번째 백엔드를 띄운다.

$victim = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 30') `
    -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 700

Save-SellerOsProcess -Component '__test_alive' -ProcessId $victim.Id -Kind 'test'
$found = Get-SellerOsProcess -Component '__test_alive'
Assert-That '실행 중인 우리 프로세스를 찾는다' ($null -ne $found -and $found.Pid -eq $victim.Id)

# ── 2. PID 재사용을 잡는다 ──────────────────────────────────────────────
# 재부팅 뒤 같은 번호를 남의 프로그램이 갖고 있을 수 있다. 시작 시각까지
# 봐야 한다 — 안 그러면 종료 스크립트가 남의 프로그램을 죽인다.

$recordPath = Get-SellerOsRecordPath -Component '__test_alive'
$record = Get-Content $recordPath -Raw | ConvertFrom-Json
$record.started_at = (Get-Date).AddDays(-3).ToString('o')
$record | ConvertTo-Json | Out-File $recordPath -Encoding utf8 -Force

Assert-That 'PID 는 같지만 시작 시각이 다르면 우리 것이 아니다' `
    ($null -eq (Get-SellerOsProcess -Component '__test_alive'))

# ── 3. 죽은 프로세스 ────────────────────────────────────────────────────

Save-SellerOsProcess -Component '__test_dead' -ProcessId $victim.Id -Kind 'test'
& taskkill.exe /PID $victim.Id /F 2>&1 | Out-Null
Start-Sleep -Milliseconds 700

Assert-That '죽은 프로세스를 실행 중이라고 하지 않는다' `
    ($null -eq (Get-SellerOsProcess -Component '__test_dead'))

Remove-SellerOsProcessRecord -Component '__test_alive'
Remove-SellerOsProcessRecord -Component '__test_dead'

# ── 4. 기록에 없는 프로세스는 죽이지 않는다 ─────────────────────────────

Assert-That '기록이 없으면 NOT_RUNNING 으로 끝난다' `
    ((Stop-SellerOsProcess -Component '__test_missing' -TimeoutSeconds 1) -eq 'NOT_RUNNING')

# ── 5. 남의 포트를 우리 것이라 하지 않는다 ──────────────────────────────
# 3000번을 다른 개발 서버가 쓰고 있는데 그걸 죽이면 사용자는 자기 작업을 잃는다.

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$busyPort = $listener.LocalEndpoint.Port
try {
    Assert-That '포트가 열린 것을 감지한다' (Test-PortListening -Port $busyPort)
    Assert-That '기록이 없는 포트를 우리 것이라 하지 않는다' `
        (-not (Test-PortOwnedBySellerOs -Port $busyPort -Component '__test_port'))
    $owner = Get-PortOwnerDescription -Port $busyPort
    Assert-That '포트 점유자를 사람이 읽을 수 있게 보여준다' `
        ($owner -ne 'none' -and $owner -notmatch 'password|DATABASE_URL') $owner
} finally {
    $listener.Stop()
}

# ── 6. worker 는 종료 표시를 따른다 ─────────────────────────────────────
# 이게 없으면 종료가 강제 종료밖에 없고, 트랜잭션 한가운데서 끊긴다.

$flag = Get-SellerOsStopFlagPath -Component 'worker'
$alreadyRunning = $null -ne (Get-SellerOsProcess -Component 'worker')
if ($alreadyRunning) {
    Write-Host "  SKIP  worker 종료 표시 (운영 worker 가 실행 중입니다)" -ForegroundColor Yellow
} else {
    Set-Content -Path $flag -Value 'test' -Encoding utf8 -Force
    try {
        $loop = Join-Path $paths.Scripts 'run_worker_loop.ps1'
        $sink = Join-Path $env:TEMP 'selleros-worker-test.log'
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $p = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $loop,
                            '-IntervalMinutes', '99') `
            -WorkingDirectory $paths.Root -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $sink -RedirectStandardError "$sink.err"
        $sw.Stop()
        # 표시가 먼저 있으면 한 바퀴도 돌지 않고 바로 끝나야 한다.
        Assert-That 'worker 가 종료 표시를 보고 즉시 끝난다' `
            ($p.ExitCode -eq 0 -and $sw.Elapsed.TotalSeconds -lt 25) `
            ("exit=$($p.ExitCode) elapsed=$([math]::Round($sw.Elapsed.TotalSeconds,1))s")
        Remove-Item $sink, "$sink.err" -Force -ErrorAction SilentlyContinue
    } finally {
        Remove-Item $flag -Force -ErrorAction SilentlyContinue
    }
}

# ── 7. 공백이 든 인자를 온전히 넘긴다 ───────────────────────────────────
# `Start-Process -ArgumentList` 는 따옴표를 안 붙인다. 실측에서 DB 점검이
# 통째로 실패한 원인이었다.

$r = Invoke-SellerOsPython -Arguments @('-c', 'print("with space ok")')
Assert-That '공백이 든 인자가 쪼개지지 않는다' `
    ($r.ExitCode -eq 0 -and $r.Stdout -match 'with space ok') `
    ("exit=$($r.ExitCode) out=$($r.Stdout)")

# ── 8. 파이썬 종료 코드를 그대로 전달한다 ───────────────────────────────
# 0/1/2 의 뜻이 다르다. 뭉개면 "일부 실패" 가 "성공" 이 된다.

$r2 = Invoke-SellerOsPython -Arguments @('-c', 'raise SystemExit(1)')
Assert-That '종료 코드 1 을 성공으로 뭉개지 않는다' ($r2.ExitCode -eq 1) "exit=$($r2.ExitCode)"

# ── 8-1. stderr 로만 떠드는 성공 명령이 스크립트를 죽이지 않는다 ────────
# 이 저장소에서 두 번 겪은 사고다. 파이썬 로깅과 npm 진행 상황이 stderr 로
# 나가고, `$ErrorActionPreference='Stop'` 이 그걸 종료 오류로 바꿨다.

$saved = $ErrorActionPreference
$ErrorActionPreference = 'Stop'
try {
    $noisy = Invoke-SellerOsPython -Arguments @(
        '-c', 'import sys; sys.stderr.write("log line\n"); print("done")')
    Assert-That 'stderr 로 떠드는 성공 명령이 스크립트를 죽이지 않는다' `
        ($noisy.ExitCode -eq 0 -and $noisy.Stdout -match 'done' -and $noisy.Stderr -match 'log line') `
        "exit=$($noisy.ExitCode)"
} catch {
    Assert-That 'stderr 로 떠드는 성공 명령이 스크립트를 죽이지 않는다' $false $_.Exception.Message
} finally {
    $ErrorActionPreference = $saved
}

# ── 9. 로그 보존 ────────────────────────────────────────────────────────
# 표준오류는 `*.log.err` 로 떨어진다. `*.log` 만 지우면 그것만 영원히 쌓인다.

$old = Join-Path $paths.LogDir '__test_old-2000-01-01.log'
$oldErr = "$old.err"
$fresh = Join-Path $paths.LogDir '__test_fresh.log'
foreach ($f in @($old, $oldErr)) {
    Set-Content -Path $f -Value 'x' -Encoding utf8 -Force
    (Get-Item $f).LastWriteTime = (Get-Date).AddDays(-90)
}
Set-Content -Path $fresh -Value 'x' -Encoding utf8 -Force

Remove-SellerOsOldLogs

Assert-That '오래된 로그를 지운다' (-not (Test-Path $old))
Assert-That '오래된 표준오류 로그도 지운다' (-not (Test-Path $oldErr))
Assert-That '최근 로그는 남긴다' (Test-Path $fresh)
Remove-Item $fresh -Force -ErrorAction SilentlyContinue

# ── 10. 점검 출력에 비밀이 없다 ─────────────────────────────────────────
# 이 출력은 로그 파일에 그대로 남는다. 여기가 마지막 방어선이다.

$pf = Invoke-SellerOsPython -Arguments @('-m', 'app.scripts.preflight', '--json')
$envValues = Read-SellerOsEnv
$leaked = @()
foreach ($key in @('JWT_SECRET', 'CREDENTIAL_MASTER_KEY', 'POSTGRES_PASSWORD')) {
    if (-not $envValues.ContainsKey($key)) { continue }
    $value = $envValues[$key]
    if ($value -and $value.Length -ge 8 -and $pf.Stdout.Contains($value)) { $leaked += $key }
}
Assert-That '환경 점검 출력에 비밀값이 없다' ($leaked.Count -eq 0) ("샘: " + ($leaked -join ', '))
Assert-That '환경 점검 출력에 접속 문자열이 없다' `
    (-not ($pf.Stdout -match 'postgresql\+psycopg://'))

# ── 결과 ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
if ($script:Failed -eq 0) {
    Write-Host "  통과 $($script:Passed) · 실패 0" -ForegroundColor Green
    Write-Host ""
    exit 0
}
Write-Host "  통과 $($script:Passed) · 실패 $($script:Failed)" -ForegroundColor Red
Write-Host ""
exit 1
