# Legacy stub -> cookie_extension_auto_install.ps1
param(
    [string]$ExtensionDir,
    [string]$Browser = 'chrome',
    [int]$DebugPort = 7897,
    [switch]$DryRun,
    [switch]$ReloadOnly,
    [switch]$Force,
    [string]$ExpectedVersion = ''
)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here 'cookie_extension_auto_install.ps1') @PSBoundParameters
