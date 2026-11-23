# Run from a source checkout, including on Windows machines with Codex's bundled Python.
$jamesRoot = (Get-Location).Path
$jamesPython = $env:JAMES_PYTHON
if (-not $jamesPython) {
    $jamesVenv = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    $jamesLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $jamesSystem = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $jamesBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $jamesVenv) { $jamesPython = $jamesVenv }
    elseif ($jamesLauncher) { $jamesPython = $jamesLauncher.Source }
    elseif ($jamesSystem -and $jamesSystem.Source -notlike '*\WindowsApps\*') { $jamesPython = $jamesSystem.Source }
    elseif (Test-Path -LiteralPath $jamesBundled) { $jamesPython = $jamesBundled }
    else { throw 'Install Python 3.10+ or set JAMES_PYTHON to its executable.' }
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $jamesPython -m james --workspace $jamesRoot @args
    $jamesExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $jamesExit
