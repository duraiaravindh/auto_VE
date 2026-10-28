[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Video,
    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Srt,
    [string]$Assets,
    [string]$OutputRoot = "projects\dynamic",
    [ValidateRange(240, 4320)]
    [int]$Width = 1080,
    [ValidateRange(240, 7680)]
    [int]$Height = 1920,
    [ValidateRange(1, 120)]
    [int]$Fps = 30,
    [switch]$NoAutoGraphics,
    [switch]$VerticalOnly
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-WorkingPython {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:NAMAKADHAI_PYTHON) {
        $candidates.Add($env:NAMAKADHAI_PYTHON)
    }
    $candidates.Add((Join-Path $scriptRoot ".venv\Scripts\python.exe"))
    $pathPython = Get-Command python -ErrorAction SilentlyContinue
    if ($pathPython) {
        $candidates.Add($pathPython.Source)
    }
    $candidates.Add("C:\Program Files\QGIS 3.44.11\apps\Python312\python.exe")

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $probeExitCode = 1
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $candidate -c "import sys; assert sys.version_info >= (3, 11)" *> $null
            $probeExitCode = $LASTEXITCODE
        }
        catch {
            $probeExitCode = 1
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        if ($probeExitCode -eq 0) {
            return $candidate
        }
    }
    throw "Python 3.11+ was not found. Set NAMAKADHAI_PYTHON to a working python.exe."
}

$videoPath = (Resolve-Path -LiteralPath $Video).Path
$srtPath = (Resolve-Path -LiteralPath $Srt).Path
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $scriptRoot $OutputRoot))
$python = Find-WorkingPython
$arguments = @(
    "-m", "namakadhai.dynamic_editor", "all", $videoPath, $srtPath,
    "--output-root", $outputPath,
    "--width", $Width,
    "--height", $Height,
    "--fps", $Fps
)
if ($Assets) {
    $arguments += @("--assets", (Resolve-Path -LiteralPath $Assets).Path)
}
if ($NoAutoGraphics) {
    $arguments += "--no-auto-graphics"
}
if (-not $VerticalOnly) {
    $arguments += "--dual-format"
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = "$scriptRoot\src;$scriptRoot"
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Dynamic render failed with exit code $LASTEXITCODE."
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
