<#
SPDX-License-Identifier: MIT

UPGRADE-177 installer glue. This script is deliberately small: it does not talk to a
database, never writes an upgrade record itself and never invents a second identity
scheme. It performs exactly two maintenance steps for the setup, both built only from
metadata that already exists on this machine:

  -Mode Prepare    Read the installed build's own published identity (qiban-payload.json
                   in the target directory), the target package's manifest, the
                   installation record and the upgrade transaction record. Decide which
                   of the four install shapes applies, ask the *frozen worker of the right
                   program* to run the existing `--upgrade-maintenance prepare` verb, and
                   only after a verified `staged` answer record the Start Menu entry that
                   can bring the previous build back. Nothing else is written.

  -Mode Finalize   After the new files were copied, remove exactly those files that the
                   previous build's own complete manifest publishes and this package does
                   not, by comparing the two manifests. Unknown files, directories,
                   links and anything outside the target directory are never touched.
                   Repeat runs remove nothing and still succeed.

Rules kept by construction (they are the reason this is a script and not more Inno code):

  * The program that runs maintenance is always
    `<app>\resources\storage\Qiban.Storage.exe` of the target directory or of the
    recorded maintenance carrier copy. `Qiban.exe` is never started here: its argument
    parser ignores unknown switches and would open the normal GUI.
  * The worker is started as one process with an argument list, never through a shell
    string, and its only answer that counts is the one the backend writes after it
    really reached `staged` - the transaction record is re-read and re-checked here.
  * An older build whose worker does not know the maintenance verb exits non-zero
    (argparse usage error, exit 2) and is reported as `upgrade_worker_unsupported`.
    The caller must then refuse to overwrite anything.
  * No key, password, credential or user content is read or written. Only relative
    published paths, digests, phases and the program directory are handled.

The result file is ASCII `key=value` lines, read by the setup with its own small
key/value reader. Keys: status (ready|refuse), branch, code, carrier, removed.
Nothing in this file is a user facing sentence: the setup owns the wording.
#>

param(
    [Parameter(Mandatory = $true)][ValidateSet('Prepare', 'Finalize')][string]$Mode,
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$InstallationRoot,
    [Parameter(Mandatory = $true)][string]$TargetIdentity,
    [Parameter(Mandatory = $true)][string]$TargetManifestPath,
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [string]$RecoveryLinkPath = '',
    [string]$WorkerExecutable = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# A setup is started from whatever environment the person happens to use, and an inherited
# PSModulePath (a PowerShell 7 one, a stray empty entry) can leave Windows PowerShell without its
# own management and utility modules - ``Get-FileHash`` and ``ConvertFrom-Json`` would simply not
# exist. The process-local path is therefore normalised to the two system directories; -NoProfile
# is passed by the caller, so no user profile is involved either way.
if ($PSVersionTable.PSVersion.Major -lt 6) {
    $systemModules = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
    $machineModules = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'
    $candidates = @()
    if (Test-Path -LiteralPath $systemModules -PathType Container) { $candidates += $systemModules }
    if (Test-Path -LiteralPath $machineModules -PathType Container) { $candidates += $machineModules }
    if ($candidates.Count -gt 0) { $env:PSModulePath = ($candidates -join ';') }
}

$IdentityName = 'qiban-payload.json'
$WorkerRelative = 'resources\storage\Qiban.Storage.exe'
$TransactionRelative = 'upgrade\transaction.json'
$ProgramsRelative = 'upgrade\programs'
$OpenPhases = @('prepared', 'staging', 'staged', 'rollback-ready', 'rolling-back',
                'emergency', 'emergency-switching')
$RecoveryPhases = @('rollback-ready', 'rolling-back', 'emergency', 'emergency-switching')
$KnownPhases = @('prepared', 'staging', 'staged', 'rollback-ready', 'rolling-back', 'confirmed',
                 'rolled-back', 'aborted', 'emergency', 'emergency-switching',
                 'emergency-rolled-back')
$DigestPattern = '^[0-9a-f]{64}$'
$IdentifierPattern = '^[0-9a-f]{32}$'
$CodePattern = '^[a-z0-9_]{1,64}$'
$MaxPath = 400

function New-Verdict {
    param([string]$Status, [string]$Branch, [string]$Code, [string]$Carrier, [int]$Removed,
          [int]$Open = 0, [int]$Skipped = 0)
    return @{ status = $Status; branch = $Branch; code = $Code; carrier = $Carrier;
              removed = $Removed; open = $Open; skipped = $Skipped }
}

function New-Ready {
    param([string]$Branch, [string]$Carrier = '', [int]$Removed = 0, [int]$Open = 0,
          [int]$Skipped = 0)
    return New-Verdict 'ready' $Branch '' $Carrier $Removed $Open $Skipped
}

function New-Refusal {
    param([string]$Code, [string]$Branch = 'refused')
    if ($Code -notmatch $CodePattern) { $Code = 'upgrade_glue_internal' }
    return New-Verdict 'refuse' $Branch $Code '' 0 0 0
}

function Write-Result {
    param([string]$Path, [hashtable]$Verdict)
    # One line per key. Each line is parenthesized on purpose: the comma operator binds tighter
    # than '+', so an un parenthesized list would concatenate into one string instead of lines.
    $lines = @(
        ('status=' + $Verdict.status),
        ('branch=' + $Verdict.branch),
        ('code=' + $Verdict.code),
        ('carrier=' + $Verdict.carrier),
        ('removed=' + $Verdict.removed),
        ('skipped=' + $Verdict.skipped),
        ('open=' + $Verdict.open)
    )
    $parent = [System.IO.Path]::GetDirectoryName($Path)
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding ASCII
}

function Get-Digest {
    # SHA-256 of one file's bytes through the framework, so no cmdlet module is involved.
    param([string]$Path)
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                         [System.IO.FileAccess]::Read,
                                         [System.IO.FileShare]::ReadWrite)
    } catch {
        return ''
    }
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $hash = $sha.ComputeHash($stream)
        } finally {
            $sha.Dispose()
        }
    } catch {
        return ''
    } finally {
        $stream.Dispose()
    }
    return [System.BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
}

function Test-PlainPath {
    # One relative published path: no drive, no leading separator, no traversal, no backslash.
    param([string]$Name)
    if ([string]::IsNullOrEmpty($Name) -or $Name.Length -gt $MaxPath) { return $false }
    if ($Name.StartsWith('/') -or $Name.Contains('\') -or $Name.Contains(':')) { return $false }
    foreach ($piece in $Name.Split('/')) {
        if ($piece -eq '' -or $piece -eq '.' -or $piece -eq '..') { return $false }
    }
    return $true
}

function Read-Manifest {
    # One published manifest: its own byte digest plus the relative paths it publishes.
    param([string]$Path, [string]$Code)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw $Code }
    $digest = Get-Digest $Path
    if ($digest -notmatch $DigestPattern) { throw $Code }
    try {
        $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
        $value = $text | ConvertFrom-Json
    } catch {
        throw $Code
    }
    if ($value -eq $null) { throw $Code }
    if (-not ($value.PSObject.Properties.Name -contains 'format')) { throw $Code }
    if ($value.format -ne 1) { throw $Code }
    $names = New-Object 'System.Collections.Generic.List[string]'
    foreach ($row in @($value.files)) {
        if ($row -eq $null) { throw $Code }
        if (-not ($row.PSObject.Properties.Name -contains 'path')) { throw $Code }
        $name = [string]$row.path
        if (-not (Test-PlainPath $name)) { throw $Code }
        $names.Add($name)
    }
    if ($names.Count -lt 1) { throw $Code }
    return @{ digest = $digest; files = $names }
}

function Read-Json {
    param([string]$Path)
    try {
        $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
        return ($text | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-Field {
    # A property of a ConvertFrom-Json object, or $null when it is absent.
    param($Value, [string]$Name)
    if ($Value -eq $null) { return $null }
    if (-not ($Value.PSObject.Properties.Name -contains $Name)) { return $null }
    return $Value.$Name
}

function Read-Transaction {
    # The upgrade record of this installation root: $null only when no file exists. A file that
    # exists but cannot be read is reported as invalid, never as "there is no transaction".
    param([string]$Root)
    $path = Join-Path $Root $TransactionRelative
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $value = Read-Json $path
    if ($value -eq $null) { return @{ invalid = $true } }
    if ((Get-Field $value 'format') -ne 1) { return @{ invalid = $true } }
    $phase = [string](Get-Field $value 'phase')
    $target = Get-Field $value 'target'
    $carrier = Get-Field $value 'carrier'
    $targetIdentity = [string](Get-Field $target 'identity_sha256')
    $carrierIdentity = [string](Get-Field $carrier 'identity_sha256')
    if ($phase -eq '' -or $targetIdentity -notmatch $DigestPattern) { return @{ invalid = $true } }
    if ($carrierIdentity -notmatch $DigestPattern) { return @{ invalid = $true } }
    return @{ invalid = $false; phase = $phase; target = $targetIdentity
              carrier = $carrierIdentity }
}

function Test-OpenPhase {
    param([string]$Phase)
    return ($OpenPhases -contains $Phase)
}

function Test-RecoveryPhase {
    param([string]$Phase)
    return ($RecoveryPhases -contains $Phase)
}

function Get-CarrierDirectory {
    param([string]$Root, [string]$CarrierIdentity)
    if ($CarrierIdentity -notmatch $DigestPattern) { return '' }
    return (Join-Path (Join-Path $Root $ProgramsRelative) $CarrierIdentity)
}

function Resolve-Worker {
    # The maintenance worker of one program directory, or '' when that program has none.
    param([string]$ProgramDirectory)
    $candidate = Join-Path $ProgramDirectory $WorkerRelative
    if ($WorkerExecutable -ne '') {
        # Test and inspection hook only: the setup never sets it. A supplied worker must
        # still live inside the program directory this step is about.
        $full = [System.IO.Path]::GetFullPath($WorkerExecutable)
        $root = [System.IO.Path]::GetFullPath($ProgramDirectory).TrimEnd('\') + '\'
        if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { return '' }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return '' }
        return $full
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return '' }
    return $candidate
}

function Invoke-Worker {
    # One worker process: argument list only, no shell string, stdout captured, stderr dropped.
    param([string]$Worker, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $standard = & $Worker @Arguments 2>$null | Out-String
        $code = $LASTEXITCODE
    } catch {
        return @{ code = -1; output = '' }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -eq $null) { $code = 0 }
    return @{ code = [int]$code; output = [string]$standard }
}

function Get-WorkerCode {
    # The stable code of a worker answer, or '' when the answer is not one JSON object.
    param([hashtable]$Result)
    try {
        $value = $Result.output | ConvertFrom-Json
    } catch {
        return ''
    }
    if ($value -eq $null) { return '' }
    $code = [string](Get-Field $value 'error')
    if ($code -match $CodePattern) { return $code }
    return ''
}

function Test-WorkerOk {
    param([hashtable]$Result)
    if ($Result.code -ne 0) { return $false }
    try {
        $value = $Result.output | ConvertFrom-Json
    } catch {
        return $false
    }
    $ok = Get-Field $value 'ok'
    return ($ok -eq $true)
}

function Test-CarrierProgram {
    # A carrier is usable only when both halves of the program are there: the window it starts and
    # the backend the window hands the request to.
    param([string]$Directory)
    if ($Directory -eq '' -or -not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Directory 'Qiban.exe') -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $Directory $WorkerRelative) -PathType Leaf)) {
        return $false
    }
    return $true
}

function Test-Carrier {
    # The recorded carrier: both program halves plus its own identity document, whose digest is the
    # identity the transaction record names. The published files themselves are not re-hashed here -
    # that is the maintenance carrier's own proof, not the installer's - but a swapped manifest can
    # never stand in for the record.
    param([string]$Directory, [string]$Identity)
    if ($Identity -notmatch $DigestPattern) { return $false }
    if (-not (Test-CarrierProgram $Directory)) { return $false }
    return ((Get-Digest (Join-Path $Directory $IdentityName)) -eq $Identity)
}

# The way back is a shortcut in the Start Menu group of this application, and that group has a
# Chinese name. WScript.Shell converts the .lnk path through the ANSI code page, so it cannot write
# that entry on a Windows installation whose code page is not Chinese; the shell's own Unicode
# interface writes it everywhere. A failure here is still reported as recovery_link_failed.
$ShortcutSource = @'
using System;
using System.Runtime.InteropServices;

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
 Guid("000214F9-0000-0000-C000-000000000046")]
public interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszFile, int cch,
                 IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszName,
                        int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszDir,
                             int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszArgs,
                      int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszIconPath,
                         int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
}

public static class QibanShortcut
{
    private static readonly Guid ShellLinkClass = new Guid("00021401-0000-0000-C000-000000000046");

    private static object Open()
    {
        return Activator.CreateInstance(Type.GetTypeFromCLSID(ShellLinkClass, true));
    }

    public static void Write(string linkPath, string targetPath, string arguments,
                             string workingDirectory)
    {
        object link = Open();
        try
        {
            var shellLink = (IShellLinkW)link;
            shellLink.SetPath(targetPath);
            shellLink.SetArguments(arguments);
            shellLink.SetWorkingDirectory(workingDirectory);
            shellLink.SetDescription("Qiban upgrade recovery");
            shellLink.SetIconLocation(targetPath, 0);
            shellLink.SetShowCmd(1);
            ((System.Runtime.InteropServices.ComTypes.IPersistFile)link).Save(linkPath, true);
        }
        finally
        {
            Marshal.ReleaseComObject(link);
        }
    }
}
'@

function Set-RecoveryLink {
    # The recorded way back: one Start Menu shortcut to the verified carrier program.
    # It is only ever written after a `staged` transaction was verified, and an existing
    # shortcut is left untouched when anything before that failed. What it points at, and with
    # which arguments, is read back by the installer tests, not checked again here.
    param([string]$LinkPath, [string]$CarrierDirectory, [string]$Root, [string]$App)
    if ($LinkPath -eq '' -or $Root -eq '' -or $App -eq '') { return $false }
    if ($Root.Contains('"') -or $App.Contains('"') -or $LinkPath.Contains('"')) { return $false }
    $target = Join-Path $CarrierDirectory 'Qiban.exe'
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $CarrierDirectory $WorkerRelative) -PathType Leaf)) {
        return $false
    }
    $parent = [System.IO.Path]::GetDirectoryName($LinkPath)
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    try {
        # Compiled on first use only, by the .NET Framework C# compiler every Windows installation
        # ships with - the host needs nothing installed. If it cannot be written, the caller refuses.
        if (([System.Management.Automation.PSTypeName]'QibanShortcut').Type -eq $null) {
            Add-Type -TypeDefinition $ShortcutSource -Language CSharp -ErrorAction Stop
        }
        [QibanShortcut]::Write($LinkPath, $target,
                               '--upgrade-recovery "' + $Root + '" "' + $App + '"', $CarrierDirectory)
    } catch {
        return $false
    }
    return (Test-Path -LiteralPath $LinkPath -PathType Leaf)
}

function Read-InstallationState {
    # @{ present = bool; ready = bool } for the installation root of this profile. A path that exists
    # as something other than a directory is present but never "absent": nothing about it is guessed.
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) {
        return @{ present = $false; ready = $false }
    }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @{ present = $true; ready = $false }
    }
    $path = Join-Path $Root 'installation.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return @{ present = $true; ready = $false }
    }
    $value = Read-Json $path
    if ($value -eq $null) { return @{ present = $true; ready = $false } }
    if ((Get-Field $value 'format') -ne 1) { return @{ present = $true; ready = $false } }
    if ([string](Get-Field $value 'phase') -ne 'ready') { return @{ present = $true; ready = $false } }
    $identifier = [string](Get-Field $value 'installation')
    if ($identifier -notmatch $IdentifierPattern) { return @{ present = $true; ready = $false } }
    return @{ present = $true; ready = $true }
}

function Set-MissingRecoveryLink {
    # The recorded way back only has to exist; an entry that is already there is left byte for byte
    # alone, because nothing here can prove it is wrong.
    param([string]$Root, [string]$App, [string]$LinkPath, [string]$CarrierIdentity)
    $carrier = Get-CarrierDirectory $Root $CarrierIdentity
    if (-not (Test-Carrier $carrier $CarrierIdentity)) { return 'upgrade_carrier_missing' }
    if (Test-Path -LiteralPath $LinkPath -PathType Leaf) { return '' }
    if (-not (Set-RecoveryLink $LinkPath $carrier $Root $App)) { return 'recovery_link_failed' }
    return ''
}

function Invoke-Prepare {
    param([string]$App, [string]$Root, [string]$Target, [string]$LinkPath)

    $identityPath = Join-Path $App $IdentityName
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
        return New-Refusal 'app_identity_missing'
    }
    $installed = Get-Digest $identityPath
    if ($installed -notmatch $DigestPattern) { return New-Refusal 'app_identity_invalid' }

    $record = Read-Transaction $Root
    if ($record -ne $null -and $record.invalid) {
        # The file exists but cannot be read. "No transaction" is never assumed for it, and nothing
        # in the program directory or the recorded way back is touched.
        return New-Refusal 'upgrade_journal_invalid'
    }
    if ($record -ne $null -and -not ($KnownPhases -contains $record.phase)) {
        # An unrecognized phase is never read as "finished": the record is not a shape this
        # installer may reason about, so nothing is overwritten.
        return New-Refusal 'upgrade_state'
    }

    if ($installed -eq $Target) {
        # Same build: a repair. It never creates or replaces a clone. A target identity only
        # constrains a transaction that is still open - a record that already finished describes a
        # build that is not installed any more and may never authorize anything here.
        if ($record -eq $null) { return New-Ready 'repair' }
        if (Test-OpenPhase $record.phase) {
            if ($record.target -ne $Target) { return New-Refusal 'upgrade_target_mismatch' 'repair' }
            if (Test-RecoveryPhase $record.phase) {
                return New-Refusal 'upgrade_recovery_pending' 'repair'
            }
            if ($record.phase -eq 'prepared' -or $record.phase -eq 'staging') {
                return New-Refusal 'upgrade_pending' 'repair'
            }
            # staged: the generations are already switched, so keep (or rebuild) the way back and
            # let the caller finish that upgrade's forward clean-up.
            $code = Set-MissingRecoveryLink $Root $App $LinkPath $record.carrier
            if ($code -ne '') { return New-Refusal $code 'repair' }
            return New-Ready 'repair' $record.carrier 0 1
        }
        if ($record.phase -eq 'confirmed') {
            # Confirmed by this very build: the way back may be kept or rebuilt, and the forward
            # clean-up may still run for a copy that stopped before it. A carrier that cannot be
            # verified here only costs the clean-up - a same-build repair never needed it.
            $code = Set-MissingRecoveryLink $Root $App $LinkPath $record.carrier
            if ($code -ne '') { return New-Ready 'repair' }
            return New-Ready 'repair' $record.carrier 0 1
        }
        # rolled-back / emergency-rolled-back / aborted: the app is the build that was installed
        # before that transaction, so its old target never authorizes a clean-up here.
        return New-Ready 'repair'
    }

    # Different build. The only legal way in is a paired upgrade transaction.
    if ($record -ne $null -and (Test-OpenPhase $record.phase)) {
        if ($record.target -ne $Target) { return New-Refusal 'upgrade_target_mismatch' 'paired' }
        if (Test-RecoveryPhase $record.phase) { return New-Refusal 'upgrade_recovery_pending' 'paired' }
        # A transaction already exists: only the recorded carrier may run maintenance.
        # The installed directory may be a mixture left by an interrupted copy, so it is
        # never executed and never used as the source of a carrier.
        $carrier = Get-CarrierDirectory $Root $record.carrier
        if (-not (Test-Carrier $carrier $record.carrier)) {
            return New-Refusal 'upgrade_carrier_missing' 'paired'
        }
        $worker = Resolve-Worker $carrier
        if ($worker -eq '') { return New-Refusal 'upgrade_worker_missing' 'carrier' }
        $branch = 'resumed'
        if ($record.phase -ne 'staged') { $branch = 'paired' }
        $program = $carrier
    } else {
        $state = Read-InstallationState $Root
        if (-not $state.present) {
            # The default profile has never created an installation: there is nothing to
            # pair with, so this is a plain program replacement. A path that exists as anything
            # else - a directory without a ready record, or a file - is NOT treated as absent.
            return New-Ready 'replace'
        }
        if (-not $state.ready) { return New-Refusal 'installation_not_ready' 'replace' }
        $worker = Resolve-Worker $App
        if ($worker -eq '') { return New-Refusal 'upgrade_worker_missing' 'paired' }
        $branch = 'paired'
        $program = $App
    }

    $arguments = @('--upgrade-maintenance', 'prepare', '--data-dir', $Root, '--app-dir', $program,
                   '--target-identity', $Target)
    $answer = Invoke-Worker $worker $arguments
    if (-not (Test-WorkerOk $answer)) {
        $code = Get-WorkerCode $answer
        if ($code -ne '') { return New-Refusal $code $branch }
        if ($answer.code -eq 2) { return New-Refusal 'upgrade_worker_unsupported' $branch }
        return New-Refusal 'upgrade_worker_failed' $branch
    }

    # The answer is not the proof: the record written by the backend is.
    $verified = Read-Transaction $Root
    if ($verified -eq $null -or $verified.invalid) { return New-Refusal 'upgrade_state' $branch }
    if ($verified.target -ne $Target) { return New-Refusal 'upgrade_target_mismatch' $branch }
    if ($verified.phase -eq 'prepared' -or $verified.phase -eq 'staging') {
        return New-Refusal 'upgrade_pending' $branch
    }
    if ($verified.phase -ne 'staged') { return New-Refusal 'upgrade_state' $branch }
    # A transaction this run just prepared (or continued) owns the entry: point it at that
    # carrier. The way back exists before a single published file is overwritten.
    $carrier = Get-CarrierDirectory $Root $verified.carrier
    if (-not (Test-Carrier $carrier $verified.carrier)) {
        return New-Refusal 'upgrade_carrier_missing' $branch
    }
    if (-not (Set-RecoveryLink $LinkPath $carrier $Root $App)) {
        return New-Refusal 'recovery_link_failed' $branch
    }
    return New-Ready $branch $verified.carrier
}

function Test-PlainAncestors {
    # Every directory on the way to one published file must be a real directory, not a link or a
    # junction: a redirected parent would make "delete this file" mean something outside the app.
    param([string]$App, [string[]]$Pieces)
    $current = $App
    for ($index = 0; $index -lt $Pieces.Count - 1; $index++) {
        $current = Join-Path $current $Pieces[$index]
        $entry = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($entry -eq $null) { return $true }
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    }
    return $true
}

function Remove-OldOnlyFiles {
    param([string]$App, [string]$Root, [string]$Target, [string]$TargetManifest)

    $record = Read-Transaction $Root
    if ($record -eq $null) { return New-Refusal 'upgrade_journal_missing' 'finalize' }
    if ($record.invalid) { return New-Refusal 'upgrade_journal_invalid' 'finalize' }
    # The copy finished for a transaction that either waits for its first start (staged) or was
    # already confirmed by it. Every other phase describes a directory this package did not write.
    if ($record.phase -ne 'staged' -and $record.phase -ne 'confirmed') {
        return New-Refusal 'upgrade_state' 'finalize'
    }
    if ($record.target -ne $Target) { return New-Refusal 'upgrade_target_mismatch' 'finalize' }
    $installed = Get-Digest (Join-Path $App $IdentityName)
    if ($installed -ne $Target) { return New-Refusal 'finalize_identity_mismatch' 'finalize' }

    try {
        $new = Read-Manifest $TargetManifest 'finalize_manifest_missing'
    } catch {
        return New-Refusal 'finalize_manifest_missing' 'finalize'
    }
    if ($new.digest -ne $Target) { return New-Refusal 'finalize_manifest_mismatch' 'finalize' }
    $carrier = Get-CarrierDirectory $Root $record.carrier
    try {
        $old = Read-Manifest (Join-Path $carrier $IdentityName) 'finalize_manifest_missing'
    } catch {
        return New-Refusal 'finalize_manifest_missing' 'finalize'
    }
    if ($old.digest -ne $record.carrier) {
        # A manifest that is not the recorded carrier's own document may never authorize a delete.
        return New-Refusal 'finalize_carrier_mismatch' 'finalize'
    }

    # Windows compares paths case-insensitively: a file that both builds publish under a
    # different letter case is not "old only" and stays.
    $published = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($name in $new.files) { [void]$published.Add($name) }
    $root = [System.IO.Path]::GetFullPath($App).TrimEnd('\') + '\'

    $removed = 0
    $skipped = 0
    foreach ($name in $old.files) {
        if ($published.Contains($name)) { continue }
        if (-not (Test-PlainPath $name)) { return New-Refusal 'finalize_path_invalid' 'finalize' }
        $pieces = $name.Split('/')
        $target = [System.IO.Path]::GetFullPath((Join-Path $App ($name.Replace('/', '\'))))
        if (-not $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            return New-Refusal 'finalize_path_invalid' 'finalize'
        }
        if (-not (Test-PlainAncestors $App $pieces)) {
            # Kept, and counted: this path is only reachable through a link.
            $skipped = $skipped + 1
            continue
        }
        $entry = Get-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        if ($entry -eq $null) { continue }
        if ($entry.PSIsContainer) { continue }
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            $skipped = $skipped + 1
            continue
        }
        try {
            Remove-Item -LiteralPath $target -Force -ErrorAction Stop
        } catch {
            return New-Refusal 'finalize_delete_failed' 'finalize'
        }
        $removed = $removed + 1
    }
    return New-Ready 'finalize' $record.carrier $removed 0 $skipped
}

# ---------------------------------------------------------------------------
# One run: normalise the inputs, run one mode, write one ASCII result, report through
# the exit code. Zero means "the caller may continue"; anything else means "refuse".
# ---------------------------------------------------------------------------

$verdict = $null
try {
    $target = ([string]$TargetIdentity).Trim().ToLowerInvariant()
    if ($null -eq (Get-Command ConvertFrom-Json -ErrorAction SilentlyContinue)) {
        # Nothing can be read without the JSON cmdlet, and guessing is worse than refusing.
        $verdict = New-Refusal 'powershell_incomplete'
    } elseif ($target -notmatch $DigestPattern) {
        $verdict = New-Refusal 'target_identity_invalid'
    } elseif ($AppDir -eq '' -or $InstallationRoot -eq '' -or $ResultPath -eq '') {
        $verdict = New-Refusal 'upgrade_glue_internal'
    } elseif ($AppDir.Contains('"') -or $InstallationRoot.Contains('"') -or $ResultPath.Contains('"')) {
        $verdict = New-Refusal 'upgrade_glue_internal'
    } elseif (-not (Test-Path -LiteralPath $AppDir -PathType Container)) {
        $verdict = New-Refusal 'upgrade_app_missing'
    } else {
        try {
            $manifest = Read-Manifest $TargetManifestPath 'target_manifest_invalid'
        } catch {
            $manifest = $null
        }
        if ($manifest -eq $null) {
            $verdict = New-Refusal 'target_manifest_invalid'
        } elseif ($manifest.digest -ne $target) {
            $verdict = New-Refusal 'target_manifest_mismatch'
        } elseif ($Mode -eq 'Prepare') {
            $verdict = Invoke-Prepare $AppDir $InstallationRoot $target $RecoveryLinkPath
        } else {
            $verdict = Remove-OldOnlyFiles $AppDir $InstallationRoot $target $TargetManifestPath
        }
    }
} catch {
    $verdict = New-Refusal 'upgrade_glue_internal'
}

if ($verdict -eq $null) { $verdict = New-Refusal 'upgrade_glue_internal' }
try {
    Write-Result $ResultPath $verdict
} catch {
    # Without a result file the caller cannot continue; the exit code below says so.
}
if ($verdict.status -eq 'ready') { exit 0 }
exit 1
