param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "status")]
    [string]$Mode = "status"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Env:PYTHONPATH = "$Root/src" + $(if ($Env:PYTHONPATH) { ";$Env:PYTHONPATH" } else { "" })

switch ($Mode) {
    "install" { & python -m orxaq_autonomy.cli --root $Root install-keepalive }
    "uninstall" { & python -m orxaq_autonomy.cli --root $Root uninstall-keepalive }
    "status" { & python -m orxaq_autonomy.cli --root $Root keepalive-status }
}

exit $LASTEXITCODE
