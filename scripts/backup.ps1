# 备份 P1 的 SQLite 数据库（WAL 模式：建议先停服再备份）
param(
    [string]$DataDir = (Join-Path $PSScriptRoot "..\data"),
    [string]$OutDir  = (Join-Path $PSScriptRoot "..\data\backups")
)
$ErrorActionPreference = "Stop"
$db = Join-Path $DataDir "visittong.db"
if (-not (Test-Path $db)) { throw "找不到数据库：$db" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zip = Join-Path $OutDir "visittong-$stamp.zip"
$tmp = Join-Path $OutDir "tmp-$stamp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Copy-Item $db (Join-Path $tmp "visittong.db")
foreach ($suffix in @("-wal", "-shm")) {
    $side = "$db$suffix"
    if (Test-Path $side) { Copy-Item $side (Join-Path $tmp ("visittong.db" + $suffix)) }
}
Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zip -Force
Remove-Item -Recurse -Force $tmp
Write-Host "backup -> $zip"