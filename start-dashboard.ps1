$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory "venv\Scripts\python.exe"
$serverExecutable = Join-Path $projectDirectory "Dorama Studio Server.exe"
$dashboardUrl = "http://127.0.0.1:8765"
$dashboardReady = $false

try {
    $dashboardResponse = Invoke-WebRequest -UseBasicParsing -Uri "$dashboardUrl/api/dashboard" -TimeoutSec 2
    $dashboardReady = $dashboardResponse.StatusCode -eq 200
} catch {
    $dashboardReady = $false
}

if (-not $dashboardReady) {
    if (Test-Path -LiteralPath $serverExecutable -PathType Leaf) {
        Start-Process -FilePath $serverExecutable `
            -ArgumentList "--no-browser" `
            -WorkingDirectory $projectDirectory
    } else {
        Start-Process -FilePath $pythonExecutable `
            -ArgumentList "-m", "webapp" `
            -WorkingDirectory $projectDirectory `
            -WindowStyle Hidden
    }

    foreach ($attempt in 1..40) {
        try {
            $dashboardResponse = Invoke-WebRequest -UseBasicParsing -Uri "$dashboardUrl/api/dashboard" -TimeoutSec 1
            if ($dashboardResponse.StatusCode -eq 200) {
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
}

Start-Process $dashboardUrl
