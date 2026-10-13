<#
.SYNOPSIS
    바탕화면에 Seller OS 바로가기를 만든다.

.DESCRIPTION
    사용자가 터미널 명령을 외우지 않게 하는 것이 목적이다.

    ```
    Seller OS 시작
    Seller OS 종료
    Seller OS 상태
    ```

    바탕화면 경로를 코드에 박지 않는다
    ------------------------------
    `[Environment]::GetFolderPath('Desktop')` 로 얻는다. OneDrive 로 옮겨진
    바탕화면, 다른 사용자 계정, 한국어/영어 Windows 에서 경로가 전부 다르다.

    `-NoExit` 를 쓰는 이유
    -------------------
    아이콘으로 실행하면 창이 뜬다. `-NoExit` 가 없으면 스크립트가 끝나는
    순간 창이 닫혀 **무엇이 실패했는지 볼 수 없다.** 종료·상태도 마찬가지다.

.PARAMETER Remove
    만들어 둔 바로가기를 지운다.

.PARAMETER Directory
    바로가기를 만들 위치. 기본은 바탕화면.
#>

param(
    [switch]$Remove,
    [string]$Directory = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$scripts = Join-Path $root 'scripts'

if (-not $Directory) { $Directory = [Environment]::GetFolderPath('Desktop') }
if (-not (Test-Path $Directory)) {
    throw "바로가기를 만들 위치를 찾지 못했습니다: $Directory"
}

$items = @(
    @{ Name = 'Seller OS 시작'; Script = 'start_seller_os.ps1';  Icon = 'shell32.dll,137' }
    @{ Name = 'Seller OS 종료'; Script = 'stop_seller_os.ps1';   Icon = 'shell32.dll,132' }
    @{ Name = 'Seller OS 상태'; Script = 'status_seller_os.ps1'; Icon = 'shell32.dll,23' }
)

if ($Remove) {
    foreach ($item in $items) {
        $path = Join-Path $Directory "$($item.Name).lnk"
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Host "  삭제  $($item.Name)" -ForegroundColor Gray
        }
    }
    Write-Host ""
    Write-Host "  바로가기를 정리했습니다." -ForegroundColor Green
    Write-Host ""
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
foreach ($item in $items) {
    $target = Join-Path $scripts $item.Script
    if (-not (Test-Path $target)) { throw "스크립트를 찾지 못했습니다: $target" }

    $lnk = $shell.CreateShortcut((Join-Path $Directory "$($item.Name).lnk"))
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    # -NoExit: 실패했을 때 창이 남아 원인을 볼 수 있어야 한다.
    $lnk.Arguments = "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$target`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = $item.Icon
    $lnk.Description = "$($item.Name) — PICKSOME Seller OS"
    $lnk.Save()
    Write-Host "  생성  $($item.Name)" -ForegroundColor Green
}

Write-Host ""
Write-Host "  위치: $Directory" -ForegroundColor Gray
Write-Host "  PC 를 켠 뒤 'Seller OS 시작' 을 한 번 누르면 됩니다." -ForegroundColor Gray
Write-Host ""
