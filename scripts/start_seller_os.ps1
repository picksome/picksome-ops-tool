<#
.SYNOPSIS
    Seller OS 시작 — 한 번 실행하면 오늘 운영을 볼 수 있는 상태까지 간다.

.DESCRIPTION
    무엇을 하나
    ----------
    ```
     1. 환경 점검        비밀값을 출력하지 않고 있다/없다만
     2. 데이터베이스      이미 떠 있으면 재사용. 실제 질의로 확인한다
     3. 스키마 리비전     코드보다 앞서 있으면 여기서 멈춘다
     4. 백엔드           이미 우리 것이 떠 있으면 재사용
     5. 백엔드 건강      포트가 아니라 /healthz (DB ping 포함)
     6. worker 루프      중복 실행 금지
     7. 프론트엔드       필요할 때만 빌드
     8. 프론트 건강
     9. 브라우저 열기
    10. 따라잡기         꺼져 있던 동안의 주문·클레임·오늘 리포트
    ```

    왜 브라우저를 따라잡기 **전에** 여는가
    ----------------------------------
    따라잡기는 벤더 API 를 상태별·창별로 부르므로 오래 걸린다(실측: 클레임
    90일치가 170회 호출 · 약 2분). 그동안 화면을 못 보게 막을 이유가 없다.

    **서비스가 떴다는 사실과 데이터가 최신이라는 사실은 다르다.** 이 구분이
    이 스크립트의 핵심이고, 그래서 브라우저는 서비스 기준으로 열고 데이터
    최신성은 따로 보고한다. 둘을 하나로 묶으면 둘 중 하나가 거짓말이 된다.

    두 번 눌러도 안전하다
    ------------------
    각 단계가 "이미 우리 것이 떠 있는가" 를 먼저 본다. PID 만 보지 않고
    시작 시각까지 대조하므로, 재부팅 뒤 같은 번호를 가진 남의 프로그램을
    우리 것으로 착각하지 않는다.

    무엇을 하지 않나
    --------------
    ```
    DB 삭제 · 초기화 · downgrade      절대
    startup 마다 seed · import        안 한다
    hot reload · 개발 모드            안 쓴다 (운영용 빌드로 띄운다)
    0.0.0.0 바인드                    안 한다 (기본 127.0.0.1)
    비밀값을 명령행에 전달             안 한다 (환경변수로만)
    ```

.PARAMETER SkipCatchup
    따라잡기를 건너뛴다. 서비스만 띄우고 싶을 때.

.PARAMETER SkipBrowser
    브라우저를 열지 않는다.

.PARAMETER SkipFrontend
    프론트엔드를 띄우지 않는다 (API 만 쓸 때).

.PARAMETER Rebuild
    변경 여부와 무관하게 프론트엔드를 다시 빌드한다.

.PARAMETER BindAddress
    백엔드·프론트가 붙을 주소. 기본 127.0.0.1 — 이 PC 밖에서는 접근할 수 없다.
#>

param(
    [switch]$SkipCatchup,
    [switch]$SkipBrowser,
    [switch]$SkipFrontend,
    [switch]$Rebuild,
    [string]$BindAddress = '',
    [int]$BackendPort = 0,
    [int]$FrontendPort = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'lib\SellerOs.psm1') -Force

$paths = Get-SellerOsPaths
Initialize-SellerOsDirs
Remove-SellerOsOldLogs

if (-not $BindAddress) {
    $BindAddress = if ($env:SELLEROS_BIND) { $env:SELLEROS_BIND } else { '127.0.0.1' }
}
if ($BackendPort -le 0) {
    $BackendPort = if ($env:SELLEROS_BACKEND_PORT) { [int]$env:SELLEROS_BACKEND_PORT } else { 8000 }
}
if ($FrontendPort -le 0) {
    $FrontendPort = if ($env:SELLEROS_FRONTEND_PORT) { [int]$env:SELLEROS_FRONTEND_PORT } else { 3000 }
}

$backendBase = "http://127.0.0.1:$BackendPort"
$frontendBase = "http://127.0.0.1:$FrontendPort"
#: 기동 후 열 화면. 오늘 무엇을 해야 하는지가 여기 있다.
$landingPath = '/operations/daily'

function Stop-StartupWithError {
    <#
    실패를 사실대로 남기고 끝낸다.

    **터미널이 바로 닫히지 않게 한다.** 아이콘으로 실행하면 창이 뜨는데,
    거기서 스크립트가 그냥 끝나면 무엇이 실패했는지 볼 시간이 없다.
    자동 실행(`SELLEROS_NONINTERACTIVE`)에서는 붙들지 않는다 — 아무도
    Enter 를 눌러 줄 수 없고 그러면 작업이 영원히 안 끝난다.
    #>
    param([string]$Message, [string]$WhatToDo)
    Write-SellerOsLog -Level 'FAIL' -Message $Message
    if ($WhatToDo) { Write-SellerOsLog -Level 'INFO' -Message "→ $WhatToDo" }
    $log = Join-Path $paths.LogDir ("startup-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
    Write-Host ""
    Write-Host "  자세한 기록: $log" -ForegroundColor Gray
    Write-Host ""
    # 아이콘으로 실행했을 때 창이 바로 닫혀 오류를 못 보는 것을 막는다.
    if ($Host.Name -eq 'ConsoleHost' -and -not $env:SELLEROS_NONINTERACTIVE) {
        Write-Host "  창을 닫으려면 Enter 를 누르세요." -ForegroundColor Gray
        [void](Read-Host)
    }
    exit 1
}

Write-Host ""
Write-Host "  Seller OS 시작" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-SellerOsLog -Level 'INFO' -Message "시작 요청 (bind=$BindAddress, backend=$BackendPort, frontend=$FrontendPort)"

# ── 1. 환경 점검 ────────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [1/10] 환경 점검"

if (-not (Test-Path $paths.EnvFile)) {
    Stop-StartupWithError ".env 파일이 없습니다 ($($paths.EnvFile))" `
        ".env.example 를 복사해 값을 채운 뒤 다시 실행하세요."
}

foreach ($tool in @('python', 'node', 'npm')) {
    $found = Get-Command $tool -ErrorAction SilentlyContinue
    if (-not $found) { Stop-StartupWithError "$tool 을(를) 찾지 못했습니다." "$tool 을 설치하고 PATH 에 넣으세요." }
}

# 디스크 여유. 백업·로그·증빙이 여기에 쌓인다. 가득 차면 pg_dump 가 0 을
# 반환하면서 잘린 파일을 남기고, 그건 복원할 때가 되어서야 드러난다.
$drive = (Get-Item $paths.Root).PSDrive
if ($drive -and ($drive.PSObject.Properties.Name -contains 'Free') -and $drive.Free) {
    if ($drive.Free -lt 2GB) {
        Write-SellerOsLog -Level 'WARN' `
            -Message ("디스크 여유 {0:N1} GB — 백업이 잘린 파일을 남길 수 있습니다" -f ($drive.Free / 1GB))
    } else {
        Write-SellerOsLog -Level 'OK' `
            -Message ("{0,-20} {1:N1} GB 여유" -f 'Disk', ($drive.Free / 1GB))
    }
}

# 값 판정은 백엔드가 한다. 여기서 .env 를 해석하지 않는다.
$preflight = Invoke-SellerOsPython -Arguments @('-m', 'app.scripts.preflight', '--json')
try { $preflightJson = $preflight.Stdout | ConvertFrom-Json } catch { $preflightJson = $null }
if ($preflightJson) {
    foreach ($check in $preflightJson.checks) {
        $level = switch ($check.status) { 'OK' { 'OK' } 'WARN' { 'WARN' } default { 'FAIL' } }
        Write-SellerOsLog -Level $level -Message ("{0,-20} {1}" -f $check.check, $check.detail)
    }
}
if ($preflight.ExitCode -ne 0) {
    Stop-StartupWithError "환경 점검을 통과하지 못했습니다." "위의 [FAIL] 항목을 .env 에서 고치세요."
}

# ── 2. 데이터베이스 ─────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [2/10] 데이터베이스"

$db = Test-SellerOsDatabase -TimeoutSeconds 5
if ($db.Ok) {
    # **이미 떠 있으면 다시 시작하지 않는다.** 이 PC 에서는 Windows 서비스가
    # STOPPED 로 보이는데도 PostgreSQL 이 붙어 있는 경우를 실제로 겪었다.
    # 서비스 상태가 아니라 실제 질의가 판정 기준이다.
    Write-SellerOsLog -Level 'OK' -Message "이미 실행 중 — 재사용합니다"
} else {
    $service = Get-Service -Name 'postgresql-16' -ErrorAction SilentlyContinue
    if (-not $service) {
        Stop-StartupWithError "데이터베이스에 접속하지 못했고, postgresql-16 서비스도 찾지 못했습니다." `
            "PostgreSQL 이 설치돼 있는지, .env 의 DATABASE_URL 이 맞는지 확인하세요."
    }
    Write-SellerOsLog -Level 'INFO' -Message "PostgreSQL 서비스를 시작합니다"
    try {
        Start-Service -Name 'postgresql-16' -ErrorAction Stop
    } catch {
        Stop-StartupWithError "PostgreSQL 서비스를 시작하지 못했습니다." `
            "관리자 권한 PowerShell 에서 'Start-Service postgresql-16' 을 실행해 보세요."
    }
    # 서비스가 RUNNING 이라고 붙는 것은 아니다. 실제 질의로 다시 확인한다.
    $db = Test-SellerOsDatabase -TimeoutSeconds 40
    if (-not $db.Ok) {
        Stop-StartupWithError "PostgreSQL 은 시작됐지만 접속에 실패했습니다." `
            ".env 의 DATABASE_URL 사용자·비밀번호·DB 이름을 확인하세요."
    }
    Write-SellerOsLog -Level 'OK' -Message "시작됨"
}

# ── 3. 스키마 리비전 ────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [3/10] 스키마 리비전"

$statusRun = Invoke-SellerOsPython `
    -Arguments @('-m', 'app.scripts.ops_status', '--json',
                 '--backup-root', (Join-Path $paths.Root 'backup'))
try { $status = $statusRun.Stdout | ConvertFrom-Json } catch { $status = $null }

if (-not $status -or -not $status.schema) {
    Stop-StartupWithError "스키마 상태를 확인하지 못했습니다." "위 로그의 오류를 확인하세요."
}
$schemaState = $status.schema.state
Write-SellerOsLog -Level 'INFO' `
    -Message ("DB {0} / 코드 {1} — {2}" -f $status.schema.db_revision, $status.schema.code_head, $schemaState)

switch ($schemaState) {
    'CURRENT' { Write-SellerOsLog -Level 'OK' -Message "DB_SCHEMA = CURRENT" }
    'BEHIND' {
        # **자동으로 올리지 않는다.** 마이그레이션은 되돌리기 어려운 변경이고,
        # 백업 없이 도는 것을 기본 동작으로 만들면 언젠가 그 대가를 치른다.
        Write-SellerOsLog -Level 'WARN' -Message "DB 스키마가 코드보다 뒤처져 있습니다."
        Write-SellerOsLog -Level 'INFO' -Message "→ 백업 후 다음을 실행하세요:"
        Write-SellerOsLog -Level 'INFO' -Message "   cd backend; python -m scripts.run_backup --kind daily"
        Write-SellerOsLog -Level 'INFO' -Message "   cd backend; python -m alembic upgrade head"
        Stop-StartupWithError "STARTUP_BLOCKED_SCHEMA_BEHIND" `
            "위 두 명령을 순서대로 실행한 뒤 다시 시작하세요."
    }
    'AHEAD' {
        # 코드를 되돌린 채 최신 DB 에 붙은 상태다. 그대로 쓰면 없는 컬럼을 읽는다.
        # downgrade 로 맞추면 데이터를 잃는다(docs/55 §5).
        Stop-StartupWithError "STARTUP_BLOCKED_SCHEMA_MISMATCH — DB 가 코드보다 앞서 있습니다." `
            "코드를 그 리비전으로 되돌리세요. DB 를 downgrade 하면 데이터를 잃습니다."
    }
    default {
        Stop-StartupWithError "STARTUP_BLOCKED_SCHEMA_UNKNOWN — 리비전을 확인하지 못했습니다." `
            "backend 에서 'python -m alembic current' 를 실행해 보세요."
    }
}

# ── 4. 백엔드 ───────────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [4/10] 백엔드"

$backend = Get-SellerOsProcess -Component 'backend'
if ($backend) {
    Write-SellerOsLog -Level 'OK' -Message "이미 실행 중 (pid $($backend.Pid)) — 재사용합니다"
} else {
    # 우리 기록은 없는데 포트가 막혀 있다. **PID 를 무작정 죽이지 않는다.**
    if (Test-PortListening -Port $BackendPort) {
        $owner = Get-PortOwnerDescription -Port $BackendPort
        Stop-StartupWithError "포트 $BackendPort 을 다른 프로그램이 쓰고 있습니다: $owner" `
            "그 프로그램을 종료하거나 -BackendPort 로 다른 포트를 지정하세요."
    }
    $log = Join-Path $paths.LogDir ("backend-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
    # `--reload` 를 쓰지 않는다. 운영 실행에 hot reload 가 있으면 파일이
    # 잠깐 저장되는 순간 프로세스가 재시작하고, 그 사이 요청이 끊긴다.
    $proc = Start-Process -FilePath 'python' `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $BindAddress,
                        '--port', "$BackendPort") `
        -WorkingDirectory $paths.Backend `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
        -WindowStyle Hidden -PassThru
    Save-SellerOsProcess -Component 'backend' -ProcessId $proc.Id -Kind 'uvicorn' -Port $BackendPort
    Write-SellerOsLog -Level 'INFO' -Message "시작됨 (pid $($proc.Id), $BindAddress`:$BackendPort)"
}

# ── 5. 백엔드 건강 ──────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [5/10] 백엔드 건강 확인"

# 포트가 아니라 /healthz 다 — DB ping 을 포함하므로 "떴지만 DB 는 못 붙음" 을 잡는다.
$health = Invoke-SellerOsHttpProbe -Url "$backendBase/healthz" -TimeoutSeconds 60 -AcceptStatus @(200)
if (-not $health.Ok) {
    Write-SellerOsLog -Level 'FAIL' -Message "/healthz 가 정상 응답하지 않습니다 ($($health.Error))"
    Stop-StartupWithError "백엔드가 준비되지 않았습니다." `
        "var\log\backend-*.log 를 확인하세요. 대개 DATABASE_URL 인증 문제입니다."
}
Write-SellerOsLog -Level 'OK' -Message "/healthz 200 (DB ping 포함)"

# ── 6. worker 루프 ──────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [6/10] worker"

$worker = Get-SellerOsProcess -Component 'worker'
if ($worker) {
    # 두 번째 worker 를 띄우지 않는다. advisory lock 이 중복 실행을 막지만,
    # 막힌 실행이 계속 쌓이면 실행 기록이 읽기 어려워진다.
    Write-SellerOsLog -Level 'OK' -Message "WORKER_ALREADY_RUNNING (pid $($worker.Pid)) — 재사용합니다"
} else {
    $loop = Join-Path $paths.Scripts 'run_worker_loop.ps1'
    $loopLog = Join-Path $paths.LogDir ("worker-console-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
    # 출력을 파일로 돌린다. 안 그러면 자식이 **이 스크립트의 stdout 핸들을
    # 물려받아** 시작 스크립트를 파이프로 읽는 쪽이 영원히 끝나지 않는다
    # (실측). 아이콘으로 실행할 때는 티가 안 나지만 자동화에서는 멈춘다.
    $proc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $loop) `
        -WorkingDirectory $paths.Root `
        -RedirectStandardOutput $loopLog -RedirectStandardError "$loopLog.err" `
        -WindowStyle Hidden -PassThru
    Save-SellerOsProcess -Component 'worker' -ProcessId $proc.Id -Kind 'worker-loop'
    Write-SellerOsLog -Level 'INFO' -Message "시작됨 (pid $($proc.Id))"
}

# ── 7. 프론트엔드 ───────────────────────────────────────────────────────
if ($SkipFrontend) {
    Write-SellerOsLog -Level 'STEP' -Message "`n  [7/10] 프론트엔드 — 건너뜀"
    Write-SellerOsLog -Level 'STEP' -Message "  [8/10] 프론트엔드 건강 확인 — 건너뜀"
} else {
    Write-SellerOsLog -Level 'STEP' -Message "`n  [7/10] 프론트엔드"

    $frontend = Get-SellerOsProcess -Component 'frontend'
    if ($frontend) {
        Write-SellerOsLog -Level 'OK' -Message "이미 실행 중 (pid $($frontend.Pid)) — 재사용합니다"
    } else {
        if (Test-PortListening -Port $FrontendPort) {
            $owner = Get-PortOwnerDescription -Port $FrontendPort
            Stop-StartupWithError "포트 $FrontendPort 을 다른 프로그램이 쓰고 있습니다: $owner" `
                "그 프로그램을 종료하거나 -FrontendPort 로 다른 포트를 지정하세요."
        }

        $buildLog = Join-Path $paths.LogDir ("frontend-build-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
        if (-not (Test-Path (Join-Path $paths.Frontend 'node_modules'))) {
            Write-SellerOsLog -Level 'INFO' -Message "node_modules 가 없습니다 — npm install (처음 한 번, 몇 분 걸립니다)"
            # npm 은 진행 상황을 stderr 로 쓴다. 파이프로 받으면 스크립트가 죽는다.
            $install = Invoke-SellerOsNpm -Arguments @('install') -TeeToFile $buildLog
            if ($install.ExitCode -ne 0) {
                Stop-StartupWithError "npm install 에 실패했습니다." "자세한 내용: $buildLog"
            }
        }

        # **매번 빌드하지 않는다.** 빌드는 몇 분이 걸리고, 소스가 그대로면
        # 결과도 그대로다. 소스가 빌드보다 새로우면 그때만 다시 만든다.
        $buildId = Join-Path $paths.Frontend '.next\BUILD_ID'
        $needBuild = $Rebuild -or (-not (Test-Path $buildId))
        if (-not $needBuild) {
            $builtAt = (Get-Item $buildId).LastWriteTime
            $newest = Get-ChildItem $paths.Frontend -Recurse -File `
                -Include '*.ts', '*.tsx', '*.css', '*.json' -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(node_modules|\.next|test-results|playwright-report)\\' } |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($newest -and $newest.LastWriteTime -gt $builtAt) {
                $needBuild = $true
                Write-SellerOsLog -Level 'INFO' -Message "소스가 빌드보다 새롭습니다 — 다시 빌드합니다"
            }
        }

        if ($needBuild) {
            Write-SellerOsLog -Level 'INFO' -Message "빌드 중 (몇 분 걸립니다)"
            try {
                $env:NODE_ENV = 'production'
                $build = Invoke-SellerOsNpm -Arguments @('run', 'build') -TeeToFile $buildLog
            } finally {
                Remove-Item Env:\NODE_ENV -ErrorAction SilentlyContinue
            }
            if ($build.ExitCode -ne 0) {
                Stop-StartupWithError "프론트엔드 빌드에 실패했습니다." "자세한 내용: $buildLog"
            }
            Write-SellerOsLog -Level 'OK' -Message "빌드 완료"
        } else {
            Write-SellerOsLog -Level 'OK' -Message "기존 빌드를 재사용합니다"
        }

        # standalone 결과물 구성. Dockerfile 이 하는 것과 같은 일이다 —
        # 배포 방식을 새로 만들지 않고 그 규약을 그대로 따른다.
        $standalone = Join-Path $paths.Frontend '.next\standalone'
        $server = Join-Path $standalone 'server.js'
        if (-not (Test-Path $server)) {
            Stop-StartupWithError "standalone 결과물이 없습니다 ($server)" `
                "-Rebuild 로 다시 실행해 보세요."
        }
        Copy-Item (Join-Path $paths.Frontend '.next\static') `
            (Join-Path $standalone '.next\static') -Recurse -Force -ErrorAction SilentlyContinue
        $public = Join-Path $paths.Frontend 'public'
        if (Test-Path $public) {
            Copy-Item $public (Join-Path $standalone 'public') -Recurse -Force -ErrorAction SilentlyContinue
        }

        $log = Join-Path $paths.LogDir ("frontend-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
        # 비밀은 명령행이 아니라 환경변수로 넘긴다. 여기 값들은 비밀이 아니지만
        # 예외를 두지 않는다 — 예외가 생기면 다음 사람이 비밀도 거기 넣는다.
        $env:PORT = "$FrontendPort"
        $env:HOSTNAME = $BindAddress
        $env:BACKEND_ORIGIN = $backendBase
        $env:NODE_ENV = 'production'
        try {
            $proc = Start-Process -FilePath 'node' -ArgumentList @('server.js') `
                -WorkingDirectory $standalone `
                -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
                -WindowStyle Hidden -PassThru
        } finally {
            foreach ($v in @('PORT', 'HOSTNAME', 'NODE_ENV')) {
                Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
            }
        }
        Save-SellerOsProcess -Component 'frontend' -ProcessId $proc.Id -Kind 'next-standalone' -Port $FrontendPort
        Write-SellerOsLog -Level 'INFO' -Message "시작됨 (pid $($proc.Id), $BindAddress`:$FrontendPort)"
    }

    # ── 8. 프론트엔드 건강 ──────────────────────────────────────────────
    Write-SellerOsLog -Level 'STEP' -Message "`n  [8/10] 프론트엔드 건강 확인"
    # 로그인 리다이렉트(307)도 정상이다. 로그인하지 않은 상태가 기본이다.
    $fe = Invoke-SellerOsHttpProbe -Url $frontendBase -TimeoutSeconds 90 `
        -AcceptStatus @(200, 302, 307, 308)
    if (-not $fe.Ok) {
        Write-SellerOsLog -Level 'FAIL' -Message "프론트엔드가 응답하지 않습니다 ($($fe.Error))"
        Stop-StartupWithError "프론트엔드가 준비되지 않았습니다." "var\log\frontend-*.log 를 확인하세요."
    }
    Write-SellerOsLog -Level 'OK' -Message "HTTP $($fe.Status)"
}

# ── 9. 브라우저 ─────────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [9/10] 브라우저"
if ($SkipBrowser -or $SkipFrontend) {
    Write-SellerOsLog -Level 'INFO' -Message "열지 않습니다"
} else {
    Start-Process "$frontendBase$landingPath" | Out-Null
    Write-SellerOsLog -Level 'OK' -Message "$frontendBase$landingPath"
}

# ── 10. 따라잡기 ────────────────────────────────────────────────────────
Write-SellerOsLog -Level 'STEP' -Message "`n  [10/10] 꺼져 있던 동안 따라잡기"

$catchupOk = $true
if ($SkipCatchup) {
    Write-SellerOsLog -Level 'INFO' -Message "건너뜀 — 데이터 최신성은 확인되지 않았습니다"
    $catchupOk = $false
} else {
    Write-SellerOsLog -Level 'INFO' -Message "주문·클레임을 읽습니다 (읽기 전용, 몇 분 걸릴 수 있습니다)"
    $catchupLog = Join-Path $paths.LogDir ("catchup-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
    $catchup = Invoke-SellerOsPython `
        -Arguments @('-m', 'app.scripts.startup_catchup') -TeeToFile $catchupLog

    # 화면에는 사람용 요약만. JSON 전문과 벤더 호출 로그는 파일에 있다 —
    # 그 안에는 계정 표시명과 URL 이 들어 있어 화면에 흘리지 않는다.
    $summaryLines = @()
    $inSummary = $false
    foreach ($line in ($catchup.Stdout -split "`r?`n")) {
        if ($line -match '기동 따라잡기 결과') { $inSummary = $true }
        if ($inSummary -and $line.Trim()) { $summaryLines += $line }
    }
    foreach ($line in $summaryLines) { Write-Host $line -ForegroundColor Gray }

    # 종료 코드 1 은 "서비스는 되는데 데이터가 최신이 아니다" 이다.
    # 이걸 성공으로 뭉개면 사용자는 낡은 숫자를 오늘 숫자로 믿는다.
    $catchupOk = ($catchup.ExitCode -eq 0)
    if ($catchup.ExitCode -ge 2) {
        Write-SellerOsLog -Level 'FAIL' -Message "따라잡기를 실행하지 못했습니다 — $catchupLog 를 보세요"
    }
}

# ── 마무리 ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "  Seller OS 시작 완료" -ForegroundColor Green
Write-Host ""
Write-Host "  화면       $frontendBase$landingPath"
Write-Host "  API        $backendBase/api/docs"
if ($catchupOk) {
    Write-Host "  데이터     최신" -ForegroundColor Green
} elseif ($SkipCatchup) {
    Write-Host "  데이터     확인 안 함 (-SkipCatchup)" -ForegroundColor Yellow
} else {
    Write-Host "  데이터     확인 필요 — 동기화가 일부 실패했습니다" -ForegroundColor Yellow
    Write-Host "             예외 센터: $frontendBase/operations/exceptions" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  종료하려면 scripts\stop_seller_os.ps1" -ForegroundColor Gray
Write-Host "  상태 확인   scripts\status_seller_os.ps1" -ForegroundColor Gray
Write-Host ""

Write-SellerOsLog -Level 'INFO' -Message "시작 완료 (데이터 최신 = $catchupOk)"
exit 0
