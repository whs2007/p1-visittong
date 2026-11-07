# 还原 P1 的 SQLite 数据库（请先停服）
param(
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [string]$DataDir = (Join-Path $PSScriptRoot "..\data")
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $BackupFile)) { throw "找不到备份：$BackupFile" }
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
$db = Join-Path $DataDir "visittong.db"
# 保留一份"还原前"现场，便于回退
if (Test-Path $db) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item $db (Join-Path $DataDir "visittong.pre-restore-$stamp.db")
}
$tmp = Join-Path $env:TEMP ("visittong-restore-" + [guid]::NewGuid().ToString("N"))
Expand-Archive -Path $BackupFile -DestinationPath $tmp -Force
Copy-Item (Join-Path $tmp "visittong.db") $db -Force
Remove-Item -Recurse -Force $tmp
foreach ($suffix in @("-wal", "-shm")) {
    $side = "$db$suffix"
    if (Test-Path $side) { Remove-Item $side -Force }
}
Write-Host "restored $BackupFile -> $db"