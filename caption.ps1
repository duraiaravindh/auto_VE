# Caption a video in one step:  .\caption.ps1 path\to\video.mp4 path\to\captions.srt
# Edited in OpenReel?  .\caption.ps1 edited.mp4 captions.srt -Original raw_recording.mp4
# Add Remotion cards:  -Intro "Remote Sandai" -IntroSubtitle "Nama Kadhai" -Outro "Nama Kadhai" -OutroSubtitle "Comment-la sollunga"
param(
    [Parameter(Mandatory = $true)][string]$Video,
    [Parameter(Mandatory = $true)][string]$Srt,
    [ValidateSet("vertical", "landscape")][string]$Layout,
    [string]$Original,
    [string]$Intro,
    [string]$IntroSubtitle = "",
    [string]$Outro,
    [string]$OutroSubtitle = ""
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
if ($Original) { $arguments += @("--original", $Original) }
if ($Intro) { $arguments += @("--intro", $Intro, "--intro-subtitle", $IntroSubtitle) }
if ($Outro) { $arguments += @("--outro", $Outro, "--outro-subtitle", $OutroSubtitle) }
& $python @arguments
exit $LASTEXITCODE
