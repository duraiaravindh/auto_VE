# Open Caption Studio in the browser:  .\studio.ps1
$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
py -3.11 -m namakadhai.caption_studio
