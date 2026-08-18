$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory "venv\Scripts\python.exe"
$dashboardUrl = "http://127.0.0.1:8765"
$dashboardReady = $false

try {
    $dashboardResponse = Invoke-WebRequest -UseBasicParsing -Uri "$dashboardUrl/api/dashboard" -TimeoutSec 2
    $dashboardReady = $dashboardResponse.StatusCode -eq 200
} catch {
    $dashboardReady = $false
}

if (-not $dashboardReady) {
    Start-Process -FilePath $pythonExecutable `
        -ArgumentList "-m", "webapp" `
        -WorkingDirectory $projectDirectory `
        -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Start-Process $dashboardUrl
