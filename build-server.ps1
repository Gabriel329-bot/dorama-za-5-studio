[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$python = Join-Path $projectRoot "venv\Scripts\python.exe"
$spec = Join-Path $projectRoot "DoramaStudioServer.spec"
$workPath = Join-Path $projectRoot "build\DoramaStudioServer"
$target = Join-Path $projectRoot "Dorama Studio Server.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Не найдено виртуальное окружение: $python"
}
if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) {
    throw "Не найден файл сборки: $spec"
}

Write-Host "Собираю единый Dorama Studio Server.exe..." -ForegroundColor Cyan
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $projectRoot `
    --workpath $workPath `
    $spec

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller завершился с кодом $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Сборка завершилась без ожидаемого файла: $target"
}

$sizeMb = [Math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1)
Write-Host "Готово: $target ($sizeMb МБ)" -ForegroundColor Green
