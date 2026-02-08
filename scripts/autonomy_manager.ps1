param(
    [Parameter(Position = 0)]
    [string]$Command = "status",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Env:PYTHONPATH = "$Root/src" + $(if ($Env:PYTHONPATH) { ";$Env:PYTHONPATH" } else { "" })

$ArgsList = @("-m", "orxaq_autonomy.cli", "--root", $Root, $Command)
if ($ExtraArgs) {
    $ArgsList += $ExtraArgs
}

& python $ArgsList
exit $LASTEXITCODE
