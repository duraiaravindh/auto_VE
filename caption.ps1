# Burn word-timed captions into a video:
#   .\caption.ps1 path\to\video.mp4 path\to\captions.srt
#   .\caption.ps1 video.mp4 captions.srt -Style config\my_style.json
param(
    [Parameter(Mandatory = $true)][string]$Video,
    [Parameter(Mandatory = $true)][string]$Srt,
    [ValidateSet("vertical", "landscape")][string]$Layout,
    [string]$Style
)
$root = $PSScriptRoot
$python = Join-Path $root ".venv-align\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Alignment environment missing: create .venv-align (see README)."
    exit 1
}
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUNBUFFERED = "1"
$arguments = @("-m", "namakadhai.caption_pipeline", $Video, $Srt)
if ($Layout) { $arguments += @("--layout", $Layout) }
if ($Style) { $arguments += @("--style", $Style) }
# Warnings from PyTorch go to stderr; don't let PowerShell treat them as failures.
$ErrorActionPreference = "Continue"
& $python @arguments
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "Captioning stopped (exit code $code). If it stopped right after step 1, close Chrome or restart the PC (low memory), then try again." -ForegroundColor Red
}
exit $code
