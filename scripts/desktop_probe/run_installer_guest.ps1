<#
SPDX-License-Identifier: MIT
UPGRADE-177 installer acceptance on one disposable Windows development guest (root runs this inside
the Hyper-V guest, never on the host). No password, no credential, no database/GUI/task/model/network
action of its own.

  -SetupPath      Qiban-Setup-<version>.exe under test (177 build)
  -EvidenceRoot   NEW absolute test directory; logs, App, Profile and result.json land below it
  -OldOverlayZip  optional zip with a complete previous shell + storage component (published files
                  only); either half may be the same file (a long-path fix shares the new backend),
                  only "both identical" counts as one frozen application, and that is recorded as
                  same_frozen_code instead of claiming two builds
  -TargetManifestPath  the rebuilt package's own published manifest; required by -ResumeUpgrade and
                  never read from whatever happens to be installed
  -ResumeUpgrade  continue only the FreshUpgrade half of the very evidence directory that stopped
                  before prepare (fresh install + old frozen initialization already succeeded there);
                  the earlier result.json is kept as result-before-resume.json, Compatibility is not
                  re-run and no profile or transaction is created or reset
  -Phases         Compatibility and/or FreshUpgrade (default: both)

Compatibility: the guest's default 176 install must be refused before anything is written (setup
exit 7, maintenance-entry refusal in the log, program and profile digests unchanged).
FreshUpgrade: fresh 177 install into <EvidenceRoot>\App with <EvidenceRoot>\Profile; optional old
overlay; synthetic old published identity; profile created by the (old) frozen storage via
--initialize; the same setup must then reach staged with the new target, keep a clone, leave a
recovery shortcut pointing at the carrier, delete only the old build's own published files, keep a
file nobody published and verify every new file; a third run must not change transaction or clone.

result.json holds phases, exit codes, digests, transaction facts and a whitelisted worker summary
(ready/installation/phase/error only - never a credential id or a configuration). Any failure writes
the result and exits non-zero without touching a later step.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [string]$OldOverlayZip = '',
    [string]$TargetManifestPath = '',
    [switch]$ResumeUpgrade,
    [ValidateSet('Compatibility', 'FreshUpgrade')][string[]]$Phases = @('Compatibility', 'FreshUpgrade')
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$OldOnlyName = 'old-only.fixture.txt'; $UnknownName = 'unknown-user.fixture.txt'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$script:Result = [ordered]@{ format = 1; ok = $false; setup = [ordered]@{}; phases = @()
    failed_step = ''; error = ''; started_at = (Get-Date).ToUniversalTime().ToString('o')
    resume = $false; resume_backup = ''; prior_compatibility = $null; prior_failure = $null }

function Add-Phase($Phase) { $script:Result.phases = @($script:Result.phases) + @($Phase) }

function Save-Result {
    $script:Result.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    [System.IO.File]::WriteAllText((Join-Path $EvidenceRoot 'result.json'), ($script:Result | ConvertTo-Json -Depth 8), $Utf8)
}

function Fail([string]$Step, [string]$Code) {
    $script:Result.failed_step = $Step; $script:Result.error = $Code; Save-Result
    Write-Host ('failed: ' + $Step + ' ' + $Code); exit 1
}

function Get-Sha256File([string]$Path) {
    $stream = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try { $sha = [System.Security.Cryptography.SHA256]::Create(); try { $hash = $sha.ComputeHash($stream) } finally { $sha.Dispose() } }
    finally { $stream.Dispose() }
    return [System.BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
}

function Get-TextDigest([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($Utf8.GetBytes($Text)) } finally { $sha.Dispose() }
    return [System.BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
}

function Read-Json([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return ([System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json) } catch { return $null }
}

function Get-Field($Value, [string]$Name) {
    if ($Value -eq $null) { return $null }
    $field = $Value.PSObject.Properties[$Name]; if ($field -eq $null) { return $null }; return $field.Value
}

function Get-ManifestPaths([string]$ManifestPath) {
    $value = Read-Json $ManifestPath; if ($value -eq $null) { return $null }
    $paths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($row in @(Get-Field $value 'files')) { $path = [string](Get-Field $row 'path'); if ($path -ne '') { $paths.Add($path) } }
    return $paths
}

function Get-ManifestHash([string]$ManifestPath, [string]$Relative) {
    $rows = @(Get-Field (Read-Json $ManifestPath) 'files')
    foreach ($row in $rows) {
        if ([string](Get-Field $row 'path') -eq $Relative) { return [string](Get-Field $row 'sha256') }
    }
    return ''
}

function Get-Digest([string]$Directory, [bool]$PublishedOnly) {
    # Published files when the directory carries a manifest, otherwise every file below it.
    $lines = New-Object 'System.Collections.Generic.List[string]'; $paths = $null
    if ($PublishedOnly) { $paths = Get-ManifestPaths (Join-Path $Directory 'qiban-payload.json') }
    if ($paths -ne $null -and $paths.Count -gt 0) {
        foreach ($relative in ($paths | Sort-Object)) {
            $full = Join-Path $Directory ($relative.Replace('/', '\'))
            if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return '' }
            $lines.Add($relative + '|' + (Get-Item -LiteralPath $full).Length + '|' + (Get-Sha256File $full))
        }
    } else {
        foreach ($item in (Get-ChildItem -LiteralPath $Directory -Recurse -File | Sort-Object FullName)) {
            $lines.Add($item.FullName.Substring($Directory.Length + 1).Replace('\', '/') + '|' + $item.Length + '|' + (Get-Sha256File $item.FullName))
        }
    }
    return Get-TextDigest ($lines -join "`n")
}

function Test-PortClosed([int]$Port) {
    if ($Port -le 0) { return $true }
    $client = New-Object System.Net.Sockets.TcpClient
    try { if ($client.ConnectAsync('127.0.0.1', $Port).Wait(400) -and $client.Connected) { return $false }; return $true }
    catch { return $true } finally { $client.Close() }
}

function Get-PortOf([string]$InstallationRoot) {
    $port = Get-Field (Read-Json (Join-Path $InstallationRoot 'installation.json')) 'port'
    if ($port -eq $null) { return 0 }; return [int]$port
}

function Assert-Idle([string]$Step) {
    foreach ($name in @('Qiban', 'Qiban.Storage')) {
        if (@(Get-Process -Name $name -ErrorAction SilentlyContinue).Count -gt 0) { Fail $Step 'application_running' }
    }
}

function Invoke-Process([string]$Exe, [string[]]$Arguments, [bool]$Capture) {
    $quoted = @()
    foreach ($argument in $Arguments) {
        if ($argument.Contains('"')) { Fail 'arguments' 'argument_quote' }
        $quoted += ('"' + $argument + '"')
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe; $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.WorkingDirectory = [System.IO.Path]::GetDirectoryName($Exe); $info.Arguments = $quoted -join ' '
    $info.RedirectStandardOutput = $Capture; $info.RedirectStandardError = $Capture
    if ($Capture) { $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8 }
    $process = [System.Diagnostics.Process]::Start($info); $output = ''
    if ($Capture) { $outputTask = $process.StandardOutput.ReadToEndAsync(); $errorsTask = $process.StandardError.ReadToEndAsync() }
    if (-not $process.WaitForExit(1800000)) { try { $process.Kill() } catch { }; return @{ code = -1; output = '' } }
    if ($Capture) { $output = $outputTask.Result; [void]$errorsTask.Result }
    $code = $process.ExitCode; $process.Dispose()
    return @{ code = $code; output = $output }
}

function Get-WorkerSummary([string]$Output) {
    # Only fixed fields may reach the evidence; a credential id or a configuration never does.
    $summary = [ordered]@{ ready = $false; installation = ''; phase = ''; error = '' }
    if ($Output.Trim() -eq '') { return $summary }
    $value = $null
    try { $value = ($Output.Trim() | ConvertFrom-Json) } catch { return $summary }
    foreach ($name in @('ready', 'installation', 'phase', 'error')) {
        $field = Get-Field $value $name
        if ($field -is [bool]) { $summary[$name] = $field } elseif ($field -is [string]) { $summary[$name] = $field }
    }
    return $summary
}

function Test-Manifest([string]$ManifestPath, [string]$App) {
    $paths = Get-ManifestPaths $ManifestPath
    if ($paths -eq $null -or $paths.Count -eq 0) { return $false }
    $rows = @{}
    foreach ($row in @(Get-Field (Read-Json $ManifestPath) 'files')) { $rows[([string](Get-Field $row 'path')).ToLowerInvariant()] = $row }
    foreach ($relative in $paths) {
        $full = Join-Path $App ($relative.Replace('/', '\')); $row = $rows[$relative.ToLowerInvariant()]
        if ($row -eq $null -or -not (Test-Path -LiteralPath $full -PathType Leaf)) { return $false }
        if ((Get-Item -LiteralPath $full).Length -ne [int64](Get-Field $row 'bytes')) { return $false }
        if ((Get-Sha256File $full) -ne [string](Get-Field $row 'sha256')) { return $false }
    }
    return $true
}

function Find-RecoveryLink([string]$InstallationRoot, [string]$Carrier) {
    # Read the installer's own Start Menu entry back; it is never created, changed or deleted here.
    if ($Carrier -eq '') { return $null }
    $expected = [System.IO.Path]::GetFullPath((Join-Path $InstallationRoot ('upgrade\programs\' + $Carrier + '\Qiban.exe')))
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    if (-not (Test-Path -LiteralPath $programs -PathType Container)) { return $null }
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($file in (Get-ChildItem -LiteralPath $programs -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
            try { $shortcut = $shell.CreateShortcut($file.FullName) } catch { continue }
            if ($shortcut.TargetPath -ne '' -and [System.IO.Path]::GetFullPath($shortcut.TargetPath) -eq $expected) {
                return @{ name = $file.Name; target = $shortcut.TargetPath }
            }
        }
        return $null
    } finally { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
}

function New-SyntheticManifest([string]$App, [string]$NewManifest, [string]$Overlay) {
    # What the mixed "previous build" really claims: the new manifest's files, everything the overlay
    # brought at its current bytes, and one file only the old build publishes. Installer bookkeeping,
    # the identity document itself and files nobody published are never listed.
    $known = New-Object 'System.Collections.Generic.List[string]'
    foreach ($path in (Get-ManifestPaths $NewManifest)) { if (-not $known.Contains($path)) { $known.Add($path) } }
    if ($Overlay -ne '') {
        foreach ($item in (Get-ChildItem -LiteralPath $Overlay -Recurse -File)) {
            $relative = $item.FullName.Substring($Overlay.Length + 1).Replace('\', '/')
            if (-not $known.Contains($relative) -and -not $relative.StartsWith('unins') -and
                $relative -ne 'qiban-payload.json' -and $relative -ne $UnknownName) { $known.Add($relative) }
        }
    }
    [System.IO.File]::WriteAllText((Join-Path $App $OldOnlyName), "only the previous build publishes this`n", $Utf8)
    if (-not $known.Contains($OldOnlyName)) { $known.Add($OldOnlyName) }
    $rows = New-Object 'System.Collections.Generic.List[object]'; $total = 0
    foreach ($relative in ($known | Sort-Object)) {
        $full = Join-Path $App ($relative.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { Fail 'overlay' ('overlay_missing_file:' + $relative) }
        $length = (Get-Item -LiteralPath $full).Length
        $rows.Add([ordered]@{ path = $relative; bytes = $length; sha256 = (Get-Sha256File $full) }); $total = $total + $length
    }
    $document = [ordered]@{ format = 1; count = $rows.Count; bytes = $total; files = $rows }
    [System.IO.File]::WriteAllText((Join-Path $App 'qiban-payload.json'), ($document | ConvertTo-Json -Depth 5 -Compress), $Utf8)
    return Get-Sha256File (Join-Path $App 'qiban-payload.json')
}

function Invoke-Compatibility {
    $phase = [ordered]@{ name = 'Compatibility'; ok = $false; setup_exit = -1; log = 'logs/compatibility.log'
        default_app = ''; default_profile = ''; program_digest = ''; profile_digest = ''; refused_before_write = $false
        program_unchanged = $false; profile_unchanged = $false }
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Qiban'; $profile = Join-Path $env:LOCALAPPDATA 'Qiban\Profile'
    $root = Join-Path $profile 'data\installation'; $phase.default_app = $app; $phase.default_profile = $profile
    if (-not (Test-Path -LiteralPath (Join-Path $root 'installation.json') -PathType Leaf)) { Add-Phase $phase; Fail 'compatibility' 'default_profile_missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $app 'qiban-payload.json') -PathType Leaf)) { Add-Phase $phase; Fail 'compatibility' 'default_app_missing' }
    $state = Read-Json (Join-Path $root 'installation.json')
    if ([string](Get-Field $state 'phase') -ne 'ready') { Add-Phase $phase; Fail 'compatibility' 'default_profile_not_ready' }
    Assert-Idle 'compatibility'
    if (-not (Test-PortClosed (Get-PortOf $root))) { Add-Phase $phase; Fail 'compatibility' 'database_running' }
    $phase.program_digest = Get-Digest $app $true; $phase.profile_digest = Get-Digest $profile $false
    if ($phase.program_digest -eq '' -or $phase.profile_digest -eq '') { Add-Phase $phase; Fail 'compatibility' 'signature_unreadable' }
    $log = Join-Path $EvidenceRoot 'logs\compatibility.log'
    $run = Invoke-Process $SetupPath @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
        ('/DIR=' + $app), ('/DataRoot=' + $profile), ('/LOG=' + $log)) $false
    $phase.setup_exit = $run.code
    $phase.program_unchanged = ($phase.program_digest -eq (Get-Digest $app $true))
    $phase.profile_unchanged = ($phase.profile_digest -eq (Get-Digest $profile $false))
    if (Test-Path -LiteralPath $log -PathType Leaf) {
        $text = [System.IO.File]::ReadAllText($log, [System.Text.Encoding]::UTF8)
        $phase.refused_before_write = ($text.Contains('upgrade_worker_unsupported') -or $text.Contains('PrepareToInstall failed'))
    }
    $phase.ok = ($run.code -eq 7) -and $phase.refused_before_write -and $phase.program_unchanged -and $phase.profile_unchanged
    Add-Phase $phase
    if (-not $phase.ok) { Fail 'compatibility' 'not_refused_or_changed' }
}

function Invoke-FreshUpgrade {
    $phase = [ordered]@{ name = 'FreshUpgrade'; ok = $false; app = 'App'; profile = 'Profile'; overlay = $false
        resumed = [bool]$ResumeUpgrade; same_frozen_code = $true; different_frozen_application = $false
        cross_schema = $false; new_manifest_digest = ''; synthetic_manifest_digest = ''
        new_shell_sha256 = ''; installed_shell_sha256 = ''; new_storage_sha256 = ''; installed_storage_sha256 = ''
        first_install_exit = -1; init_ready = $false; init_summary = [ordered]@{}; upgrade_exit = -1
        repair_exit = -1; transaction = ''; clone = ''; target = ''; carrier = ''; staged = $false
        recovery_link = ''; recovery_target = ''; clone_unchanged = $false; program_verified = $false
        old_only_removed = $false; unknown_kept = $false; database_stopped = $false
        installation_before = ''; installation_after = ''; installation_unchanged = $false }
    $app = Join-Path $EvidenceRoot 'App'; $profile = Join-Path $EvidenceRoot 'Profile'
    $root = Join-Path $profile 'data\installation'
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', ('/DIR=' + $app), ('/DataRoot=' + $profile))
    Assert-Idle 'upgrade'
    if ($ResumeUpgrade) {
        # The existing evidence must be a FreshUpgrade that stopped before prepare: fresh install
        # and the old frozen initialization succeeded, no transaction exists and the pointer never
        # moved. The installed identifier is captured here and must survive the whole run.
        $prior = $script:Result.prior_failure
        $priorPhase = $null
        foreach ($entry in @(Get-Field $prior 'phases')) { if ([string](Get-Field $entry 'name') -eq 'FreshUpgrade') { $priorPhase = $entry } }
        if ($priorPhase -eq $null -or [string](Get-Field $prior 'failed_step') -ne 'upgrade') {
            Add-Phase $phase; Fail 'resume' 'prior_failure_not_in_upgrade'
        }
        if ([int](Get-Field $priorPhase 'first_install_exit') -ne 0 -or -not [bool](Get-Field $priorPhase 'init_ready')) {
            Add-Phase $phase; Fail 'resume' 'prior_stage_incomplete'
        }
        $phase.first_install_exit = [int](Get-Field $priorPhase 'first_install_exit')
        $phase.init_ready = [bool](Get-Field $priorPhase 'init_ready')
        $phase.init_summary = Get-Field $priorPhase 'init_summary'
        $state = Read-Json (Join-Path $root 'installation.json')
        $identifier = [string](Get-Field $state 'installation')
        if ([string](Get-Field $state 'phase') -ne 'ready' -or $identifier -eq '') { Add-Phase $phase; Fail 'resume' 'installation_not_ready' }
        if (-not (Test-Path -LiteralPath $app -PathType Container) -or -not (Test-Path -LiteralPath $profile -PathType Container)) {
            Add-Phase $phase; Fail 'resume' 'app_or_profile_missing'
        }
        if (Test-Path -LiteralPath (Join-Path $root 'upgrade\transaction.json')) { Add-Phase $phase; Fail 'resume' 'transaction_present' }
        $phase.installation_before = $identifier
    } else {
        $fresh = Invoke-Process $SetupPath ($arguments + @('/LOG=' + (Join-Path $EvidenceRoot 'logs\fresh.log'))) $false
        $phase.first_install_exit = $fresh.code
        if ($fresh.code -ne 0) { Add-Phase $phase; Fail 'upgrade' ('fresh_install_exit:' + $fresh.code) }
    }
    $manifest = Join-Path $EvidenceRoot 'new-payload.json'
    if ($TargetManifestPath -ne '') {
        # A rebuilt package: its manifest is handed in, never read from whatever is installed now.
        if (Test-Path -LiteralPath $manifest -PathType Leaf) {
            Copy-Item -LiteralPath $manifest -Destination (Join-Path $EvidenceRoot 'new-payload-before-resume.json') -Force
        }
        Copy-Item -LiteralPath $TargetManifestPath -Destination $manifest -Force
    } elseif (-not $ResumeUpgrade) {
        Copy-Item -LiteralPath (Join-Path $app 'qiban-payload.json') -Destination $manifest -Force
    } else { Add-Phase $phase; Fail 'resume' 'target_manifest_required' }
    $phase.new_manifest_digest = Get-Sha256File $manifest
    # The package's own shell and backend hashes come from its manifest, so a resume (where nothing
    # new was installed) compares exactly the same two files as a first run does.
    $phase.new_shell_sha256 = Get-ManifestHash $manifest 'Qiban.exe'
    $phase.new_storage_sha256 = Get-ManifestHash $manifest 'resources/storage/Qiban.Storage.exe'
    $overlay = ''
    if ($OldOverlayZip -ne '') {
        $overlay = [System.IO.Path]::GetFullPath((Join-Path $EvidenceRoot 'overlay'))
        $evidenceBoundary = [System.IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\') + '\'
        if (-not $overlay.StartsWith($evidenceBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
            Fail 'overlay' 'overlay_outside_evidence'
        }
        if (Test-Path -LiteralPath $overlay) { Remove-Item -LiteralPath $overlay -Recurse -Force }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($OldOverlayZip, $overlay)
        foreach ($item in (Get-ChildItem -LiteralPath $overlay -Recurse -File)) {
            $destination = Join-Path $app $item.FullName.Substring($overlay.Length + 1)
            New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($destination)) | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
        }
        $phase.overlay = $true
    }
    $phase.installed_shell_sha256 = Get-Sha256File (Join-Path $app 'Qiban.exe')
    $phase.installed_storage_sha256 = Get-Sha256File (Join-Path $app 'resources\storage\Qiban.Storage.exe')
    # Either half may legitimately be the same file (a long-path fix shares the new backend); only
    # "both identical" means this really is one frozen application. Nothing here fakes a version.
    $phase.different_frozen_application = ($phase.installed_shell_sha256 -ne $phase.new_shell_sha256) -or
        ($phase.installed_storage_sha256 -ne $phase.new_storage_sha256)
    $phase.same_frozen_code = -not $phase.different_frozen_application
    if ($phase.overlay -and -not $phase.different_frozen_application) {
        Add-Phase $phase; Fail 'overlay' 'overlay_not_a_different_application'
    }
    $phase.synthetic_manifest_digest = New-SyntheticManifest $app $manifest $overlay
    [System.IO.File]::WriteAllText((Join-Path $app $UnknownName), "user content nobody published`n", $Utf8)
    if (-not $ResumeUpgrade) {
        $init = Invoke-Process (Join-Path $app 'resources\storage\Qiban.Storage.exe') @('--initialize', $root,
            '--postgres', (Join-Path $app 'resources\postgres\bin')) $true
        $summary = Get-WorkerSummary $init.output; $phase.init_summary = $summary
        $phase.init_ready = ($init.code -eq 0) -and $summary.ready
        if (-not $phase.init_ready) { Add-Phase $phase; Fail 'initialize' ('worker_exit:' + $init.code + ':' + $summary.error) }
        if (-not (Test-PortClosed (Get-PortOf $root))) { Add-Phase $phase; Fail 'initialize' 'database_still_running' }
    }
    $upgrade = Invoke-Process $SetupPath ($arguments + @('/LOG=' + (Join-Path $EvidenceRoot 'logs\upgrade.log'))) $false
    $phase.upgrade_exit = $upgrade.code
    if ($upgrade.code -ne 0) { Add-Phase $phase; Fail 'upgrade' ('upgrade_exit:' + $upgrade.code) }
    $record = Read-Json (Join-Path $root 'upgrade\transaction.json')
    if ($record -eq $null) { Add-Phase $phase; Fail 'upgrade' 'transaction_missing' }
    $phase.transaction = [string](Get-Field $record 'id'); $phase.carrier = [string](Get-Field (Get-Field $record 'carrier') 'identity_sha256')
    $phase.target = [string](Get-Field (Get-Field $record 'target') 'identity_sha256')
    $clone = Get-Field $record 'clone'; if ($clone -ne $null) { $phase.clone = [string](Get-Field $clone 'generation') }
    $phase.staged = (([string](Get-Field $record 'phase')) -eq 'staged') -and ($phase.target -eq $phase.new_manifest_digest) -and
        ($phase.clone -ne '') -and ($phase.carrier -eq $phase.synthetic_manifest_digest)
    $link = Find-RecoveryLink $root $phase.carrier
    if ($link -ne $null) { $phase.recovery_link = $link.name; $phase.recovery_target = $link.target }
    if (-not $phase.staged) { Add-Phase $phase; Fail 'upgrade' 'not_staged_or_wrong_target' }
    if ($link -eq $null) { Add-Phase $phase; Fail 'upgrade' 'recovery_link_missing' }
    $phase.program_verified = Test-Manifest $manifest $app
    $phase.old_only_removed = -not (Test-Path -LiteralPath (Join-Path $app $OldOnlyName))
    $phase.unknown_kept = Test-Path -LiteralPath (Join-Path $app $UnknownName) -PathType Leaf
    $before = $phase.transaction + '/' + $phase.clone
    $repair = Invoke-Process $SetupPath ($arguments + @('/LOG=' + (Join-Path $EvidenceRoot 'logs\repair.log'))) $false
    $phase.repair_exit = $repair.code
    $again = Read-Json (Join-Path $root 'upgrade\transaction.json')
    if ($again -ne $null) {
        $againClone = Get-Field $again 'clone'; $value = ''
        if ($againClone -ne $null) { $value = [string](Get-Field $againClone 'generation') }
        $phase.clone_unchanged = ($before -eq ([string](Get-Field $again 'id') + '/' + $value))
    }
    $phase.program_verified = $phase.program_verified -and (Test-Manifest $manifest $app)
    if ($ResumeUpgrade) {
        # The retry must stay the same installation reading the same schema set: no re-init, no new
        # profile, no transaction the record would have to be reset for.
        $state = Read-Json (Join-Path $root 'installation.json')
        $phase.installation_after = [string](Get-Field $state 'installation')
        $phase.installation_unchanged = ($phase.installation_after -eq $phase.installation_before)
        if (-not $phase.installation_unchanged) { Add-Phase $phase; Fail 'resume' 'installation_changed' }
    }
    $phase.database_stopped = Test-PortClosed (Get-PortOf $root)
    $phase.ok = ($phase.repair_exit -eq 0) -and $phase.clone_unchanged -and $phase.program_verified -and
        $phase.old_only_removed -and $phase.unknown_kept -and $phase.database_stopped
    $phase.cross_schema = $false   # one schema set on both sides: this batch is not a cross-schema run
    Add-Phase $phase
    if (-not $phase.ok) { Fail 'upgrade' 'final_state_not_verified' }
}

try {
    if (-not [System.IO.Path]::IsPathRooted($EvidenceRoot)) { Write-Host 'failed: arguments evidence_root_not_absolute'; exit 1 }
    if (-not [System.IO.Path]::IsPathRooted($SetupPath)) { Fail 'arguments' 'setup_not_absolute' }
    if ($ResumeUpgrade) {
        # A resume only continues the FreshUpgrade half on the very evidence directory that stopped
        # there; the earlier result is kept beside the new one and never overwritten.
        $Phases = @('FreshUpgrade')
        if (-not (Test-Path -LiteralPath $EvidenceRoot -PathType Container)) { Write-Host 'failed: arguments resume_evidence_missing'; exit 1 }
        $priorPath = Join-Path $EvidenceRoot 'result.json'
        if (-not (Test-Path -LiteralPath $priorPath -PathType Leaf)) { Fail 'arguments' 'resume_prior_result_missing' }
        $prior = Read-Json $priorPath
        if ($prior -eq $null -or [string](Get-Field $prior 'failed_step') -ne 'upgrade') { Fail 'arguments' 'resume_prior_not_upgrade_failure' }
        foreach ($name in @('Compatibility')) {
            foreach ($entry in @(Get-Field $prior 'phases')) {
                if ([string](Get-Field $entry 'name') -eq $name) { $script:Result.prior_compatibility = $entry }
            }
        }
        $script:Result.prior_failure = $prior; $script:Result.resume = $true
        $backup = Join-Path $EvidenceRoot 'result-before-resume.json'
        if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $priorPath -Destination $backup -Force }
        $script:Result.resume_backup = 'result-before-resume.json'
        if ($TargetManifestPath -eq '') { Fail 'arguments' 'target_manifest_required' }
        if (-not (Test-Path -LiteralPath (Join-Path $EvidenceRoot 'App') -PathType Container)) { Fail 'arguments' 'resume_app_missing' }
        if (-not (Test-Path -LiteralPath (Join-Path $EvidenceRoot 'Profile') -PathType Container)) { Fail 'arguments' 'resume_profile_missing' }
    } else {
        if (Test-Path -LiteralPath $EvidenceRoot) { Write-Host 'failed: arguments evidence_root_exists'; exit 1 }
        New-Item -ItemType Directory -Force -Path (Join-Path $EvidenceRoot 'logs') | Out-Null
    }
    if (-not (Test-Path -LiteralPath $SetupPath -PathType Leaf)) { Fail 'arguments' 'setup_missing' }
    if ($TargetManifestPath -ne '' -and -not (Test-Path -LiteralPath $TargetManifestPath -PathType Leaf)) {
        Fail 'arguments' 'target_manifest_missing'
    }
    if ($OldOverlayZip -ne '' -and -not (Test-Path -LiteralPath $OldOverlayZip -PathType Leaf)) { Fail 'arguments' 'overlay_zip_missing' }
    $script:Result.setup = [ordered]@{ path = $SetupPath; bytes = (Get-Item -LiteralPath $SetupPath).Length
        sha256 = (Get-Sha256File $SetupPath); phases = @($Phases); overlay_zip = ($OldOverlayZip -ne '')
        target_manifest = $TargetManifestPath; target_manifest_sha256 = '' }
    if ($TargetManifestPath -ne '') { $script:Result.setup.target_manifest_sha256 = Get-Sha256File $TargetManifestPath }
    Save-Result
    foreach ($name in $Phases) {
        if ($name -eq 'Compatibility') { Invoke-Compatibility }
        if ($name -eq 'FreshUpgrade') { Invoke-FreshUpgrade }
    }
    $script:Result.ok = $true; Save-Result
    Write-Host ('ok: ' + ($Phases -join ',')); exit 0
} catch {
    $script:Result.failed_step = 'script'; $script:Result.error = 'unexpected:' + $_.Exception.GetType().Name
    Save-Result
    Write-Host ('failed: script ' + $script:Result.error); exit 1
}
