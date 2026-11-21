param([string]$OutputDirectory, [switch]$ManagedApplication, [switch]$SubscriptionValidation)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$packageCache=Join-Path $projectRoot '.dev/refactor-r0/desktop/nuget'
$output=if ($OutputDirectory) { [System.IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $projectRoot '.dev/refactor-r0/desktop/portable' }
$recorder=Join-Path $PSScriptRoot 'build_desktop_sources.py'
# Prefer an explicit interpreter, then the project environment, then PATH: no personal path.
$python=$env:QIBAN_DESKTOP_PYTHON
if (-not $python) {
    $localPython=Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { $python=$localPython }
    else {
        $found=Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $found) { throw 'Python not found: set QIBAN_DESKTOP_PYTHON to a python.exe' }
        $python=$found.Source
    }
}
$sourceSnapshot=[System.IO.Path]::GetTempFileName()
$oldCache=$env:NUGET_PACKAGES
$oldTelemetry=$env:DOTNET_CLI_TELEMETRY_OPTOUT
try {
    $env:NUGET_PACKAGES=$packageCache
    $env:DOTNET_CLI_TELEMETRY_OPTOUT='1'
    $defines=if($ManagedApplication){'QIBAN_MANAGED_APPLICATION'}else{''}
    if($SubscriptionValidation){
        if(-not $ManagedApplication){throw 'SubscriptionValidation requires ManagedApplication'}
        $defines += '%3BQIBAN_SIWC_VALIDATION'
    }
    # Record the desktop sources before publishing; the same whitelist is re-read after it.
    & $python $recorder pre --project $projectRoot --snapshot $sourceSnapshot
    if ($LASTEXITCODE -ne 0) { throw 'Desktop source snapshot failed; see the message above' }
    & dotnet publish (Join-Path $PSScriptRoot 'Qiban.DesktopProbe.csproj') -c Release -p:RestoreLockedMode=true "-p:DefineConstants=$defines" -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true -p:DebugType=None -p:BaseIntermediateOutputPath=../../.dev/refactor-r0/desktop/obj/ -o $output -v minimal -warnaserror
    if ($LASTEXITCODE -ne 0) { throw 'Desktop probe publish failed' }
    if($ManagedApplication){ '{"mode":"managed","format":1}' | Set-Content -LiteralPath (Join-Path $output 'managed-application.json') -Encoding utf8 }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses/WebView2-SDK.txt') -Destination $output
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses/Windows-SDK.rtf') -Destination $output
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses/CsWinRT-LICENSE.txt') -Destination $output
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $output 'Qiban-LICENSE.txt')
    $desktop=Join-Path $packageCache 'microsoft.windowsdesktop.app.runtime.win-x64/10.0.14/LICENSE'
    Copy-Item -LiteralPath $desktop -Destination (Join-Path $output 'dotnet-desktop-LICENSE.txt')
    $core=Join-Path $packageCache 'microsoft.netcore.app.runtime.win-x64/10.0.14'
    Copy-Item -LiteralPath (Join-Path $core 'LICENSE.TXT') -Destination (Join-Path $output 'dotnet-runtime-LICENSE.txt')
    Copy-Item -LiteralPath (Join-Path $core 'THIRD-PARTY-NOTICES.TXT') -Destination (Join-Path $output 'dotnet-runtime-THIRD-PARTY-NOTICES.txt')
    $record=@($recorder, 'post', '--project', $projectRoot, '--snapshot', $sourceSnapshot, '--app-dir', $output)
    if($ManagedApplication){ $record += '--managed' }
    if($SubscriptionValidation){ $record += '--subscription' }
    & $python @record
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build record failed: sources changed or the published shell is inconsistent' }
} finally {
    Remove-Item -LiteralPath $sourceSnapshot -Force -ErrorAction SilentlyContinue
    $env:NUGET_PACKAGES=$oldCache
    $env:DOTNET_CLI_TELEMETRY_OPTOUT=$oldTelemetry
}
