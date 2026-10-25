param([switch]$Check)
$ErrorActionPreference = 'Stop'
$qqProject = Split-Path -Parent $PSScriptRoot
$qqVendor = Join-Path $qqProject '.dev/qq/vendor/NapCat-Node-4.18.41'
$qqRuntime = Join-Path $qqProject '.dev/qq/runtime'
$qqArchive = Join-Path $qqProject '.dev/qq/vendor/NapCat.Shell.Windows.Node-v4.18.41.zip'
$qqIntent = Get-Content -LiteralPath (Join-Path $qqProject '.dev/qq/module.json') -Raw | ConvertFrom-Json
if ($qqIntent.account -notmatch '^[1-9][0-9]{4,19}$') { throw 'qq_configuration' }
if ((Get-FileHash -LiteralPath $qqArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne
    '51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0') { throw 'qq_bridge_archive_mismatch' }
foreach ($qqRelative in @('node.exe','index.js','wrapper.node','napcat/napcat.mjs')) {
    if (-not (Test-Path -LiteralPath (Join-Path $qqVendor $qqRelative) -PathType Leaf)) { throw 'qq_bridge_missing' }
}
foreach ($qqRelative in @('config/webui.json',('config/onebot11_' + $qqIntent.account + '.json'))) {
    if (-not (Test-Path -LiteralPath (Join-Path $qqRuntime $qqRelative) -PathType Leaf)) { throw 'qq_bridge_configuration_missing' }
}
if ($Check) {
    @{offline_ready=$true;login_verified=$false;role_started=$false;messages_sent=0} | ConvertTo-Json -Compress
    exit 0
}
if (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @(3000,3001,6099) }) {
    throw 'qq_bridge_port_in_use_check_existing_instance'
}
$qqEnvironment = @{
    NAPCAT_WORKDIR=$qqRuntime; NAPCAT_DISABLE_TIME_SYNC='1'; NAPCAT_DISABLE_BYPASS='1';
    NAPCAT_DISABLE_MULTI_PROCESS='1'; NAPCAT_DISABLE_PIPE='1';
    NAPCAT_PATCH_PACKAGE=$null; NAPCAT_LOAD_PATH=$null; NAPCAT_INJECT_PATH=$null;
    NAPCAT_MAIN_PATH=$null; NAPCAT_LAUNCHER_PATH=$null
}
$qqPrevious = @{}
try {
    foreach ($qqName in $qqEnvironment.Keys) {
        $qqPrevious[$qqName] = [Environment]::GetEnvironmentVariable($qqName,'Process')
        [Environment]::SetEnvironmentVariable($qqName,$qqEnvironment[$qqName],'Process')
    }
    $qqEntry = Join-Path $qqVendor 'index.js'
    $qqProcess = Start-Process -FilePath (Join-Path $qqVendor 'node.exe') -WorkingDirectory $qqVendor `
        -ArgumentList @(('"' + $qqEntry + '"'),'-q',$qqIntent.account) -WindowStyle Hidden -PassThru
    $qqRecord = @{pid=$qqProcess.Id;account=$qqIntent.account;started=$qqProcess.StartTime.ToUniversalTime().ToString('o')}
    $qqRecord | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $qqRuntime 'node-launcher.json') -Encoding UTF8
    @{launcher_pid=$qqProcess.Id;login_verified=$false;role_started=$false;messages_sent=0} | ConvertTo-Json -Compress
} finally {
    foreach ($qqName in $qqPrevious.Keys) {
        [Environment]::SetEnvironmentVariable($qqName,$qqPrevious[$qqName],'Process')
    }
}
