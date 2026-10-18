# Caption a video in one step:  .\caption.ps1 path\to\video.mp4 path\to\captions.srt
param(
    [Parameter(Mandatory = $true)][string]$Video,
    [Parameter(Mandatory = $true)][string]$Srt,
    [ValidateSet("vertical", "landscape")][string]$Layout
)
$root = $PSScriptRoot
$python = Join-Path $root ".venv-align\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Alignment environment missing: create .venv-align (see README, 'Captioning a video')."
    exit 1
}
$env:PYTHONPATH = Join-Path $root "src"
$arguments = @("-m", "namakadhai.caption_pipeline", $Video, $Srt)
if ($Layout) { $arguments += @("--layout", $Layout) }
& $python @arguments
exit $LASTEXITCODE
