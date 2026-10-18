# Start the OpenReel editor locally and open it in the browser:  .\openreel.ps1
# OpenReel lives outside OneDrive (its node_modules has ~100k files); see README.
param(
    [string]$OpenReelDir = "$env:USERPROFILE\dev\openreel-video",
    [int]$Port = 5173
)
if (-not (Test-Path (Join-Path $OpenReelDir "package.json"))) {
    Write-Error "OpenReel not found at $OpenReelDir. See README, 'Editing in OpenReel'."
    exit 1
}
$url = "http://localhost:$Port/"
try {
    Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-Host "OpenReel is already running at $url"
    Start-Process $url
    exit 0
} catch {}
Write-Host "Starting OpenReel at $url (Ctrl+C to stop)..."
Start-Job -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $u; return } catch { Start-Sleep 1 }
    }
} -ArgumentList $url | Out-Null
Set-Location $OpenReelDir
npx -y pnpm@11.7.0 --filter "@openreel/web" exec vite --port $Port --strictPort
