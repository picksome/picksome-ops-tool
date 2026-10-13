<#
.SYNOPSIS
    Seller OS 로컬 운영 공용 함수.

.DESCRIPTION
    시작·종료·상태 스크립트가 함께 쓰는 것만 모았다.

    여기에 업무 판단을 넣지 않는다
    ---------------------------
    "데이터가 최신인가", "스키마가 맞는가" 같은 판단은 전부 백엔드
    파이썬(`app/scripts/ops_status.py`, `preflight.py`)이 한다. 같은 판정을
    셸에도 두면 두 곳이 언젠가 갈라지고, 그때 어느 쪽이 맞는지 아무도 모른다.

    이 모듈이 하는 일은 Windows 에서만 필요한 것들이다.

        프로세스가 살아 있나 · 그게 우리 것인가
        포트를 누가 쓰고 있나
        로그를 어디에 쌓고 언제 지우나
        .env 를 읽어 자식 프로세스에 넘긴다

    나중에 서버로 옮기면 이 파일만 systemd/compose 로 바뀐다.

    비밀을 다루는 규칙
    ----------------
    · `.env` 값을 화면에도 로그에도 쓰지 않는다. 있다/없다까지만
    · 비밀을 명령행 인자로 넘기지 않는다 (프로세스 목록에 보인다).
      전부 자식 프로세스의 **환경변수**로만 전달한다
#>

Set-StrictMode -Version Latest

# ─────────────────────────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────────────────────────

function Get-SellerOsRoot {
    <# 저장소 루트. 스크립트 위치에서 올라간다 — 사용자 경로를 박지 않는다. #>
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-SellerOsPaths {
    $root = Get-SellerOsRoot
    return [pscustomobject]@{
        Root      = $root
        Backend   = Join-Path $root 'backend'
        Frontend  = Join-Path $root 'frontend'
        Scripts   = Join-Path $root 'scripts'
        # 실행 상태(pid 기록)와 로그. 둘 다 저장소에 커밋하지 않는다.
        RunDir    = Join-Path $root 'var\run'
        LogDir    = Join-Path $root 'var\log'
        BackupDir = Join-Path $root 'backup'
        EnvFile   = Join-Path $root '.env'
    }
}

function Initialize-SellerOsDirs {
    $p = Get-SellerOsPaths
    foreach ($d in @($p.RunDir, $p.LogDir)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
}

# ─────────────────────────────────────────────────────────────────────────
# 로그
# ─────────────────────────────────────────────────────────────────────────

#: 로그 보존 일수. 무제한으로 쌓지 않는다.
$script:LogRetentionDays = 14

function Write-SellerOsLog {
    <#
    .SYNOPSIS
        화면과 파일에 같은 줄을 남긴다.
    .DESCRIPTION
        터미널이 닫혀도 무엇이 실패했는지 남아야 한다. 반대로 로그만 남고
        화면에 안 보이면 사용자는 멈춘 줄 안다. 그래서 둘 다 쓴다.
    #>
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'OK', 'WARN', 'FAIL', 'STEP')][string]$Level = 'INFO',
        [string]$LogName = 'startup'
    )
    Initialize-SellerOsDirs
    $p = Get-SellerOsPaths
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $file = Join-Path $p.LogDir ("{0}-{1}.log" -f $LogName, (Get-Date -Format 'yyyy-MM-dd'))
    "$stamp [$Level] $Message" | Out-File -FilePath $file -Append -Encoding utf8

    $prefix = switch ($Level) {
        'OK'   { '  [OK]   ' }
        'WARN' { '  [WARN] ' }
        'FAIL' { '  [FAIL] ' }
        'STEP' { '' }
        default { '         ' }
    }
    $color = switch ($Level) {
        'OK'   { 'Green' }
        'WARN' { 'Yellow' }
        'FAIL' { 'Red' }
        'STEP' { 'Cyan' }
        default { 'Gray' }
    }
    Write-Host "$prefix$Message" -ForegroundColor $color
}

function Remove-SellerOsOldLogs {
    <# 보존 기간이 지난 로그를 지운다. 무제한 증가 금지. #>
    $p = Get-SellerOsPaths
    if (-not (Test-Path $p.LogDir)) { return }
    $cutoff = (Get-Date).AddDays(-$script:LogRetentionDays)
    # `*.log*` 로 잡는다. 표준오류는 `*.log.err` 로 떨어지는데 `*.log` 만
    # 지우면 그 파일들만 영원히 쌓인다.
    Get-ChildItem $p.LogDir -Filter '*.log*' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# ─────────────────────────────────────────────────────────────────────────
# .env
# ─────────────────────────────────────────────────────────────────────────

function Read-SellerOsEnv {
    <#
    .SYNOPSIS
        루트 `.env` 를 hashtable 로 읽는다. **값을 출력하지 않는다.**
    .DESCRIPTION
        백엔드는 `app/config.py` 가 `.env` 를 절대경로로 직접 읽으므로 이
        함수가 필요 없다. 필요한 쪽은 **프론트엔드와 worker 루프**다 —
        Node 프로세스는 이 파일을 모른다.
    #>
    $p = Get-SellerOsPaths
    $out = @{}
    if (-not (Test-Path $p.EnvFile)) { return $out }
    foreach ($line in Get-Content $p.EnvFile -Encoding utf8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        # 따옴표로 감싼 값 처리. 안쪽 내용은 건드리지 않는다.
        if ($value.Length -ge 2 -and (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $out[$key] = $value
    }
    return $out
}

# ─────────────────────────────────────────────────────────────────────────
# 프로세스 기록 — "우리 것인가" 를 판정하기 위한 최소 정보
# ─────────────────────────────────────────────────────────────────────────

function Get-SellerOsRecordPath {
    param([Parameter(Mandatory)][string]$Component)
    return (Join-Path (Get-SellerOsPaths).RunDir "$Component.json")
}

function Save-SellerOsProcess {
    <#
    .SYNOPSIS
        시작한 프로세스를 기록한다.
    .DESCRIPTION
        PID 만 적으면 부족하다. PID 는 재사용된다 — 재부팅 뒤 같은 번호를
        전혀 다른 프로그램이 갖고 있을 수 있고, 그걸 보고 종료 스크립트가
        남의 프로그램을 죽인다. 그래서 **시작 시각**을 함께 적고, 확인할 때
        둘 다 맞아야 우리 것으로 본다.
    #>
    param(
        [Parameter(Mandatory)][string]$Component,
        [Parameter(Mandatory)][int]$ProcessId,
        [string]$Kind = '',
        [int]$Port = 0
    )
    Initialize-SellerOsDirs
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    $started = $null
    if ($proc) {
        try { $started = $proc.StartTime.ToString('o') } catch { $started = $null }
    }
    $record = [ordered]@{
        component  = $Component
        pid        = $ProcessId
        kind       = $Kind
        port       = $Port
        started_at = $started
        recorded_at = (Get-Date).ToString('o')
    }
    $record | ConvertTo-Json | Out-File -FilePath (Get-SellerOsRecordPath $Component) `
        -Encoding utf8 -Force
}

function Get-SellerOsProcess {
    <#
    .SYNOPSIS
        기록된 프로세스가 **지금도 우리 것인지** 확인해서 돌려준다.
    .DESCRIPTION
        기록이 없거나, PID 가 죽었거나, 살아 있어도 시작 시각이 다르면
        `$null` 을 돌려준다. 마지막 경우가 PID 재사용이다.

        여기서 프로세스를 죽이지 않는다 — 판정만 한다.
    #>
    param([Parameter(Mandatory)][string]$Component)

    $path = Get-SellerOsRecordPath $Component
    if (-not (Test-Path $path)) { return $null }
    try { $record = Get-Content $path -Raw -Encoding utf8 | ConvertFrom-Json }
    catch { return $null }
    if (-not $record.pid) { return $null }

    $proc = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }

    if ($record.started_at) {
        try {
            $recorded = [datetime]::Parse($record.started_at)
            # 1초 이내면 같은 프로세스로 본다. 직렬화 정밀도 차이를 흡수한다.
            if ([math]::Abs(($proc.StartTime - $recorded).TotalSeconds) -gt 1) {
                return $null   # PID 재사용. 남의 프로그램이다.
            }
        } catch {
            return $null
        }
    }
    return [pscustomobject]@{
        Component = $Component
        Pid       = [int]$record.pid
        Kind      = [string]$record.kind
        Port      = [int]$record.port
        Process   = $proc
    }
}

function Remove-SellerOsProcessRecord {
    param([Parameter(Mandatory)][string]$Component)
    Remove-Item (Get-SellerOsRecordPath $Component) -Force -ErrorAction SilentlyContinue
}

function Get-SellerOsStopFlagPath {
    <#
    worker 에게 "이번 바퀴까지만 하고 멈춰라" 를 알리는 파일.

    Windows 에는 콘솔 프로세스에 보낼 마땅한 종료 신호가 없다. `taskkill`
    은 창 없는 프로세스를 곱게 세우지 못하고(실측: "could not be
    terminated"), `/F` 는 트랜잭션 한가운데를 끊는다. 파일 한 개가 가장
    단순하고 확실한 협조 방식이다.
    #>
    param([Parameter(Mandatory)][string]$Component)
    return (Join-Path (Get-SellerOsPaths).RunDir "$Component.stop")
}

function Invoke-Taskkill {
    <#
    `taskkill` 을 부르되 **PowerShell 의 오류 스트림을 건드리지 않는다.**

    Windows PowerShell 5.1 은 네이티브 명령의 stderr 를 ErrorRecord 로 감싼다.
    `$ErrorActionPreference = 'Stop'` 인 스크립트에서 그게 나오면 종료 절차가
    첫 줄에서 멈춘다 — 실제로 겪었다. 남은 프로세스를 정리하는 스크립트가
    첫 실패에서 죽으면 나머지는 영영 안 내려간다.

    그래서 별도 프로세스로 띄우고 출력은 파일로 보낸다.
    #>
    param([Parameter(Mandatory)][int]$ProcessId, [switch]$Force)
    $arguments = @('/PID', "$ProcessId", '/T')
    if ($Force) { $arguments += '/F' }
    $sink = Join-Path $env:TEMP ("selleros-taskkill-{0}.log" -f $ProcessId)
    try {
        Start-Process -FilePath 'taskkill.exe' -ArgumentList $arguments `
            -NoNewWindow -Wait -RedirectStandardOutput $sink `
            -RedirectStandardError "$sink.err" -ErrorAction Stop | Out-Null
    } catch {
        # 이미 죽었거나 권한이 없다. 어느 쪽이든 다음 확인이 판정한다.
    } finally {
        Remove-Item $sink, "$sink.err" -Force -ErrorAction SilentlyContinue
    }
}

function Stop-SellerOsProcess {
    <#
    .SYNOPSIS
        기록된 프로세스를 **되도록 곱게** 종료한다.
    .DESCRIPTION
        순서가 있다.

        ```
        1. 멈춤 표시 파일       (-UseStopFlag 일 때만) 스스로 끝낼 기회를 준다
        2. taskkill /T          자식까지 포함해 종료 요청
        3. taskkill /T /F       제한 시간이 지나야. 마지막 수단이다
        ```

        1 을 모두에게 쓰지 않는 이유
        -------------------------
        협조가 필요한 것은 **worker 뿐**이다. worker 는 자기 주기를 도는 동안
        쓰기 작업을 하므로, 한가운데서 끊기면 실행 기록이 RUNNING 으로 남고
        그걸 치우는 일이 다음 기동의 몫이 된다.

        백엔드(uvicorn)와 프론트엔드(node)는 창이 없는 콘솔 프로세스라
        Windows 에서 곱게 세울 방법이 마땅치 않다. 대신 **그럴 필요가 없다** —
        요청 처리 중에 끊기면 PostgreSQL 이 연결 종료와 함께 트랜잭션을
        되돌린다. 반쯤 쓰인 상태가 남지 않는다. 그래서 이 둘은 표시 파일을
        기다리지 않고 바로 종료 절차로 간다(기다려 봐야 시간만 쓴다).

        기록에 없는 PID 는 건드리지 않는다. 포트를 쓴다는 이유만으로도
        죽이지 않는다 — 남의 프로그램일 수 있다.
    #>
    param(
        [Parameter(Mandatory)][string]$Component,
        [int]$TimeoutSeconds = 20,
        [switch]$UseStopFlag
    )
    $found = Get-SellerOsProcess -Component $Component
    if (-not $found) {
        Remove-SellerOsProcessRecord -Component $Component
        return 'NOT_RUNNING'
    }

    if ($UseStopFlag) {
        $flag = Get-SellerOsStopFlagPath -Component $Component
        $graceful = $false
        try {
            Initialize-SellerOsDirs
            Set-Content -Path $flag -Value (Get-Date).ToString('o') -Encoding utf8 -Force
            # 스스로 끝낼 시간을 준다. 전체 제한의 절반까지만 기다린다 —
            # 나머지 절반은 강제 절차에 남겨 둔다.
            $softDeadline = (Get-Date).AddSeconds([math]::Max(2, $TimeoutSeconds / 2))
            while ((Get-Date) -lt $softDeadline) {
                if (-not (Get-Process -Id $found.Pid -ErrorAction SilentlyContinue)) {
                    $graceful = $true
                    break
                }
                Start-Sleep -Milliseconds 400
            }
        } finally {
            Remove-Item $flag -Force -ErrorAction SilentlyContinue
        }

        if ($graceful) {
            Remove-SellerOsProcessRecord -Component $Component
            return 'GRACEFUL'
        }
    }

    # taskkill /T 는 자식까지 본다. next/uvicorn 은 자식 프로세스를 만든다.
    Invoke-Taskkill -ProcessId $found.Pid
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $found.Pid -ErrorAction SilentlyContinue)) {
            Remove-SellerOsProcessRecord -Component $Component
            return 'STOPPED'
        }
        Start-Sleep -Milliseconds 400
    }

    # 마지막 수단. 여기까지 왔다는 것은 곱게 끝낼 방법이 없다는 뜻이다.
    Invoke-Taskkill -ProcessId $found.Pid -Force
    Start-Sleep -Milliseconds 600
    Remove-SellerOsProcessRecord -Component $Component
    if (Get-Process -Id $found.Pid -ErrorAction SilentlyContinue) { return 'STILL_RUNNING' }
    return 'KILLED'
}

# ─────────────────────────────────────────────────────────────────────────
# 포트
# ─────────────────────────────────────────────────────────────────────────

function Get-PortOwnerPid {
    <# 그 포트를 듣고 있는 프로세스 ID. 없으면 $null. #>
    param([Parameter(Mandatory)][int]$Port)
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    return [int]$conn.OwningProcess
}

function Test-PortListening {
    param([Parameter(Mandatory)][int]$Port)
    return $null -ne (Get-PortOwnerPid -Port $Port)
}

function Get-PortOwnerDescription {
    <#
    .SYNOPSIS
        포트를 쓰는 프로그램을 사람이 읽을 수 있게. **명령행은 넣지 않는다.**
    .DESCRIPTION
        명령행에는 접속 문자열이 섞여 있을 수 있고 이 문자열은 로그에 남는다.
        이름과 PID 면 사용자가 작업 관리자에서 찾기에 충분하다.
    #>
    param([Parameter(Mandatory)][int]$Port)
    $ownerPid = Get-PortOwnerPid -Port $Port
    if (-not $ownerPid) { return 'none' }
    $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if (-not $proc) { return "pid $ownerPid" }
    return ("{0} (pid {1})" -f $proc.ProcessName, $ownerPid)
}

function Test-PortOwnedBySellerOs {
    <#
    .SYNOPSIS
        그 포트를 우리가 띄운 프로세스가 쓰고 있는가.
    .DESCRIPTION
        **PID 만 보고 죽이지 않기 위한 함수다.** 3000번을 다른 개발 서버가
        쓰고 있는데 그걸 Seller OS 로 착각해 죽이면, 사용자는 자기 작업을
        잃는다. 우리 기록과 대조해서 확실할 때만 참이다.

        자식 프로세스도 인정한다 — `npm`/`node` 는 부모가 포트를 직접 듣지
        않을 수 있다.
    #>
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Component
    )
    $ownerPid = Get-PortOwnerPid -Port $Port
    if (-not $ownerPid) { return $false }
    $ours = Get-SellerOsProcess -Component $Component
    if (-not $ours) { return $false }
    if ($ownerPid -eq $ours.Pid) { return $true }

    # 부모를 따라 올라가며 우리 PID 가 나오는지 본다.
    $cursor = $ownerPid
    for ($depth = 0; $depth -lt 5; $depth++) {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId = $cursor" -ErrorAction SilentlyContinue
        if (-not $info -or -not $info.ParentProcessId) { return $false }
        if ([int]$info.ParentProcessId -eq $ours.Pid) { return $true }
        $cursor = [int]$info.ParentProcessId
    }
    return $false
}

# ─────────────────────────────────────────────────────────────────────────
# 건강 확인
# ─────────────────────────────────────────────────────────────────────────

function Invoke-SellerOsHttpProbe {
    <#
    .SYNOPSIS
        HTTP 로 살아 있는지 확인한다. 포트가 열렸다는 것만으로는 부족하다.
    .DESCRIPTION
        포트는 프로세스가 뜨자마자 열리지만, 그때 DB 는 아직 안 붙어 있을 수
        있다. 그 상태를 HEALTHY 로 적으면 사용자는 열린 화면에서 500 을 본다.
    #>
    param(
        [Parameter(Mandatory)][string]$Url,
        [int]$TimeoutSeconds = 60,
        [int[]]$AcceptStatus = @(200, 307, 302)
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = 'no attempt'
    while ((Get-Date) -lt $deadline) {
        try {
            $res = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10 `
                -MaximumRedirection 0 -ErrorAction Stop
            if ($AcceptStatus -contains [int]$res.StatusCode) {
                return [pscustomobject]@{ Ok = $true; Status = [int]$res.StatusCode; Error = $null }
            }
            $lastError = "status $([int]$res.StatusCode)"
        } catch {
            # 리다이렉트는 WebException 으로 온다. 그것도 "살아 있다" 이다.
            $status = $null
            if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
                try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = $null }
            }
            if ($status -and $AcceptStatus -contains $status) {
                return [pscustomobject]@{ Ok = $true; Status = $status; Error = $null }
            }
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 800
    }
    return [pscustomobject]@{ Ok = $false; Status = $null; Error = $lastError }
}

function Invoke-SellerOsProcess {
    <#
    .SYNOPSIS
        외부 명령을 부르고 표준출력·표준오류·종료코드를 돌려준다.
    .DESCRIPTION
        왜 `& cmd ... 2>&1` 을 쓰지 않나
        -----------------------------
        Windows PowerShell 5.1 은 `2>&1` 로 합친 네이티브 stderr 를
        **ErrorRecord 로** 파이프라인에 흘린다. `$ErrorActionPreference =
        'Stop'` 인 스크립트에서는 그것이 종료 오류가 된다 — 명령이 성공해도
        **로그 한 줄만 stderr 로 나오면 스크립트가 그 자리에서 죽는다.**

        실제로 두 번 겪었다.

        ```
        따라잡기      파이썬 로깅 첫 줄에서 중단
        종료 스크립트  taskkill 첫 경고에서 죽어 백엔드·프론트를 남김
        ```

        `npm` 도 같은 부류다(진행 상황을 stderr 로 쓴다). 그래서 별도
        프로세스로 띄우고 두 스트림을 각각 파일로 받는다.

        종료 코드를 그대로 살려 오는 것이 중요하다. 이 저장소의 CLI 들은
        0/1/2 로 뜻을 구분한다 — 뭉개면 "일부 실패" 가 "성공" 이 된다.

        출력은 UTF-8 로 읽는다. 한글 메시지가 콘솔 코드페이지에 깨져 판정이
        틀어지는 것을 막는다.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkingDirectory = '',
        [string]$TeeToFile = ''
    )
    if (-not $WorkingDirectory) { $WorkingDirectory = (Get-SellerOsPaths).Root }

    # `Start-Process -ArgumentList` 는 배열을 **공백으로 잇기만 하고 따옴표를
    # 붙이지 않는다.** 공백이 든 인자(`-c "코드"`, 공백이 든 경로)가 그대로
    # 쪼개져 엉뚱한 명령이 된다 — 실측으로 DB 점검이 통째로 실패했다.
    $quoted = foreach ($a in $Arguments) {
        if ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' } else { $a }
    }
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $quoted `
            -WorkingDirectory $WorkingDirectory -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $code = $proc.ExitCode
    } catch {
        Remove-Item $out, $err -Force -ErrorAction SilentlyContinue
        return [pscustomobject]@{ ExitCode = 2; Stdout = ''; Stderr = "$($_.Exception.Message)" }
    }

    $stdout = if (Test-Path $out) { Get-Content $out -Raw -Encoding utf8 } else { '' }
    $stderr = if (Test-Path $err) { Get-Content $err -Raw -Encoding utf8 } else { '' }
    Remove-Item $out, $err -Force -ErrorAction SilentlyContinue

    if ($TeeToFile) {
        # 진단은 파일에 다 남긴다. 화면에는 호출부가 고른 것만 나간다.
        "$stdout`n$stderr" | Out-File -FilePath $TeeToFile -Append -Encoding utf8
    }
    return [pscustomobject]@{ ExitCode = $code; Stdout = $stdout; Stderr = $stderr }
}

function Invoke-SellerOsPython {
    <# backend 디렉터리에서 파이썬 모듈을 부른다. #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$TeeToFile = ''
    )
    return Invoke-SellerOsProcess -FilePath 'python' -Arguments $Arguments `
        -WorkingDirectory (Get-SellerOsPaths).Backend -TeeToFile $TeeToFile
}

function Invoke-SellerOsNpm {
    <#
    frontend 디렉터리에서 npm 을 부른다.

    `npm` 은 `.cmd` 라 `Start-Process` 가 직접 못 띄운다. `cmd /c` 로 감싼다.
    진행 상황을 stderr 로 쓰므로 `Invoke-SellerOsProcess` 를 지나야 한다.
    #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$TeeToFile = ''
    )
    return Invoke-SellerOsProcess -FilePath "$env:ComSpec" `
        -Arguments (@('/c', 'npm') + $Arguments) `
        -WorkingDirectory (Get-SellerOsPaths).Frontend -TeeToFile $TeeToFile
}

function Test-SellerOsDatabase {
    <#
    .SYNOPSIS
        **실제 질의**로 DB 를 확인한다. 프로세스가 있다고 HEALTHY 로 치지 않는다.
    .DESCRIPTION
        이 판정은 실측에서 나왔다. 이 PC 에서는 Windows 서비스가 STOPPED 로
        보이는데도 PostgreSQL 이 붙어 있었다. 서비스 상태만 봤으면 멀쩡한 DB 를
        다시 띄우려 했을 것이다.

        접속 정보는 백엔드가 `.env` 에서 읽는다 — 여기서 비밀번호를 만지지 않는다.
    #>
    param([int]$TimeoutSeconds = 20)
    $probe = 'from app.db.session import SessionLocal;' `
        + 'from sqlalchemy import text;' `
        + "s = SessionLocal(); s.execute(text('SELECT 1')); s.close(); print('DB_OK')"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $r = Invoke-SellerOsPython -Arguments @('-c', $probe)
        if ($r.ExitCode -eq 0 -and $r.Stdout -match 'DB_OK') {
            return [pscustomobject]@{ Ok = $true; Error = $null }
        }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Milliseconds 1000
    }
    # 예외 문구에 접속 문자열이 섞일 수 있다. **원문을 돌려주지 않는다.**
    return [pscustomobject]@{ Ok = $false; Error = 'connection failed' }
}

Export-ModuleMember -Function `
    Get-SellerOsRoot, Get-SellerOsPaths, Initialize-SellerOsDirs, `
    Write-SellerOsLog, Remove-SellerOsOldLogs, Read-SellerOsEnv, `
    Save-SellerOsProcess, Get-SellerOsProcess, Remove-SellerOsProcessRecord, `
    Get-SellerOsRecordPath, Get-SellerOsStopFlagPath, Invoke-Taskkill, `
    Stop-SellerOsProcess, Get-PortOwnerPid, Test-PortListening, `
    Get-PortOwnerDescription, Test-PortOwnedBySellerOs, `
    Invoke-SellerOsHttpProbe, Invoke-SellerOsProcess, Invoke-SellerOsPython, `
    Invoke-SellerOsNpm, Test-SellerOsDatabase
