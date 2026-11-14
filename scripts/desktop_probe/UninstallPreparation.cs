// SPDX-License-Identifier: MIT
using System.Diagnostics;

namespace Qiban.DesktopProbe;

// One uninstall preparation run. The application owns the preparation page, the person's own
// backup decision and its own orderly shutdown; every actual deletion stays with the installer's
// own registered uninstaller (unins???.exe), which this program only starts once, and only after
// that person explicitly continued. Nothing here deletes, moves or renames anything: a directory
// that carries no registered uninstaller is described, never removed recursively.
internal enum UninstallBackupState { None, Running, Succeeded, Failed, Skipped }

internal sealed class UninstallSession
{
    internal UninstallSession(string programDirectory, string dataRoot, string? uninstallerPath,
        bool backupAvailable, string backupUnavailableReason = "")
    {
        ProgramDirectory = programDirectory;
        DataRoot = dataRoot;
        UninstallerPath = uninstallerPath;
        BackupAvailable = backupAvailable;
        BackupUnavailableReason = backupUnavailableReason;
    }

    // The program directory a registered uninstaller has to belong to; nothing outside it is ever
    // started, and this program never removes anything inside it either.
    internal string ProgramDirectory { get; }
    // The profile root, which holds personal data, backups and credentials. It is outside the
    // program directory and is not touched by the preparation at all.
    internal string DataRoot { get; }
    internal string? UninstallerPath { get; }
    internal bool Registered => UninstallerPath is not null;
    // Whether this installation has local data a backup could capture at all. A profile that never
    // created an installation gets no backup offer, so skipping a backup never creates a database.
    internal bool BackupAvailable { get; }
    internal string BackupUnavailableReason { get; }
    internal UninstallBackupState Backup { get; private set; } = UninstallBackupState.None;
    internal string BackupName { get; private set; } = "";
    internal string BackupDetail { get; private set; } = "";
    internal bool HandedOff { get; private set; }
    internal bool Busy => Backup == UninstallBackupState.Running;

    // A successful backup of this run, or the person's own explicit decision to go on without one.
    // A refused, cancelled or failed backup never counts as either.
    internal bool CanContinue => Registered
        && Backup is UninstallBackupState.Succeeded or UninstallBackupState.Skipped;

    internal void BeginBackup()
    {
        if (Busy) throw new InvalidOperationException("uninstall_backup_busy");
        Backup = UninstallBackupState.Running;
        BackupName = "";
        BackupDetail = "";
    }

    internal void BackupSucceeded(string name)
    {
        Backup = UninstallBackupState.Succeeded;
        BackupName = name ?? "";
        BackupDetail = "";
    }

    // A cancelled or failed attempt keeps the page in place: the result stays remembered as it was,
    // so nothing can claim a backup that did not happen.
    internal void BackupFailed(string code, string message)
    {
        Backup = UninstallBackupState.Failed;
        BackupName = "";
        BackupDetail = string.IsNullOrWhiteSpace(message) ? code : message;
    }

    internal void SkipBackup()
    {
        if (Busy) throw new InvalidOperationException("uninstall_backup_busy");
        Backup = UninstallBackupState.Skipped;
        BackupName = "";
        BackupDetail = "";
    }

    // Why continuing is refused right now, or "" when the person may continue. The page and the
    // host read this one answer, so a disabled button and a refused request never disagree.
    internal string ContinueRefusal()
    {
        if (HandedOff) return "uninstall_already_started";
        if (!Registered) return "uninstaller_missing";
        if (Busy) return "uninstall_backup_running";
        return CanContinue ? "" : "uninstall_backup_incomplete";
    }

    internal void MarkHandedOff() => HandedOff = true;
}

// The fixed wiring of the published uninstall entry, of the registered uninstaller lookup and of
// the one parameter that tells the installer's own uninstaller that preparation already happened.
internal static class UninstallRule
{
    // Byte-identical alias of the published application; see assemble_application.py.
    internal const string AliasName = "uninstall.exe";
    internal const string ApplicationName = "Qiban.exe";
    internal const string PreparationSwitch = "--uninstall";
    // Inno keeps one uninstaller per application id: unins000.exe, unins001.exe, ...
    internal const string UninstallerPrefix = "unins";
    internal const string UninstallerSuffix = ".exe";
    // Fixed internal parameter. Only the preparation page's own hand-off helper passes it, and only
    // to the uninstaller this program resolved inside its own program directory.
    internal const string PreparedParameter = "/QIBANPREPARED=1";
    // The installer registers one fixed application id for every build (see installer/Qiban.iss).
    internal const string RegistrationKey =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\{9C1F0A53-2C4E-4A7B-9B1E-6D2F8A5C1E74}_is1";
    internal const string UninstallValue = "UninstallString";
    // Two fixed internal helper modes. Each takes only the process id it waits for: the action and
    // the directory come from the helper's own location, so nothing here accepts a command.
    internal const string OpenPreparationHelper = "--open-uninstall";
    internal const string FinishHelper = "--finish-uninstall";

    internal static bool IsHelper(string? argument) =>
        argument is OpenPreparationHelper or FinishHelper;

    // uninstall.exe only forwards to the neighbouring application; it never runs a second copy of
    // itself and never carries a flag a person would have to remember.
    internal static bool IsAlias(string? fileName) =>
        string.Equals(fileName, AliasName, StringComparison.OrdinalIgnoreCase);

    internal static string AliasTarget(string programDirectory) =>
        Path.Combine(programDirectory, ApplicationName);

    internal static string[] AliasArguments() => [PreparationSwitch];

    internal static string[] HandoffArguments() => [PreparedParameter];

    // Only use the current user's registered uninstaller inside this program directory. Every candidate must be an
    // unins???.exe inside the program directory, so a directory that was only unpacked (or copied
    // from another machine) stays unrecognised.
    internal static string? ResolveUninstaller(string programDirectory, Func<string?>? registration)
    {
        var program = Path.TrimEndingDirectorySeparator(Path.GetFullPath(programDirectory));
        var candidates = new[] { registration?.Invoke() };
        foreach (var candidate in candidates)
        {
            if (string.IsNullOrWhiteSpace(candidate)) continue;
            try
            {
                var path = Path.GetFullPath(candidate);
                if (!IsUninstallerName(Path.GetFileName(path))) continue;
                if (!string.Equals(Path.GetDirectoryName(path), program, StringComparison.OrdinalIgnoreCase)
                    || !File.Exists(path)) continue;
                return path;
            }
            catch (Exception error) when (error is ArgumentException or NotSupportedException
                or PathTooLongException)
            {
                // A candidate that is not a usable path is simply not this installation's entry.
            }
        }
        return null;
    }

    internal static bool IsUninstallerName(string? fileName)
    {
        if (string.IsNullOrEmpty(fileName)
            || !fileName.EndsWith(UninstallerSuffix, StringComparison.OrdinalIgnoreCase)) return false;
        var stem = fileName[..^UninstallerSuffix.Length];
        return stem.Length == UninstallerPrefix.Length + 3
            && stem.StartsWith(UninstallerPrefix, StringComparison.OrdinalIgnoreCase)
            && stem[UninstallerPrefix.Length..].All(char.IsAsciiDigit);
    }

    // True only for a path that really lies inside the given directory; a sibling such as
    // "Qiban-Other" never counts as inside "Qiban".
    internal static bool IsInside(string directory, string path)
    {
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(directory))
            + Path.DirectorySeparatorChar;
        return Path.GetFullPath(path).StartsWith(root, StringComparison.OrdinalIgnoreCase);
    }

    // The per-user registration the installer writes; a quoted value is the documented shape.
    internal static string? RegisteredUninstaller()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RegistrationKey);
            return key?.GetValue(UninstallValue) as string is { } value ? Unquoted(value) : null;
        }
        catch (Exception error) when (error is System.Security.SecurityException
            or UnauthorizedAccessException or IOException or ObjectDisposedException)
        {
            // An unreadable registration is simply not an entry this run may use.
            return null;
        }
    }

    internal static string? Unquoted(string value)
    {
        var text = value.Trim();
        if (text.StartsWith('"'))
        {
            var end = text.IndexOf('"', 1);
            return end > 1 ? text[1..end] : null;
        }
        var space = text.IndexOf(' ');
        return space < 0 ? text : text[..space];
    }

    // Start the registered uninstaller of this installation with the fixed internal parameter.
    internal static bool StartRegisteredUninstaller(string programDirectory, string path)
    {
        if (!IsUninstallerName(Path.GetFileName(path)) || !IsInside(programDirectory, path)
            || !File.Exists(path)) return false;
        return Start(path, programDirectory, HandoffArguments());
    }

    // Start the preparation page of this installation.
    internal static bool StartPreparationPage(string programDirectory,string? profile=null) =>
        Start(AliasTarget(programDirectory), programDirectory,
            profile is null?AliasArguments():[PreparationSwitch,"--data-dir",profile]);

    static bool Start(string file, string workingDirectory, string[] arguments)
    {
        if (!File.Exists(file)) return false;
        var start = new ProcessStartInfo(file) { UseShellExecute = false, WorkingDirectory = workingDirectory };
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        try
        {
            using var process = Process.Start(start);
            return process is not null;
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception
            or InvalidOperationException or ObjectDisposedException or IOException)
        {
            return false;
        }
    }

    // What the preparation page says for a directory without a registered uninstaller.
    internal const string PortableDirectoryGuidance =
        "没有找到本机登记的卸载程序，这个目录看起来是免安装（直接解压）的运行目录。\n\n"
        + "保存备份后，正常退出栖伴，再在文件资源管理器里手动删除程序文件夹即可。\n\n"
        + "个人资料、备份与凭据都保存在程序目录之外的资料目录里，删除程序目录不会删除它们。";
}

// The one internal helper this application starts for itself. It waits for the process that asked
// for it to exit (so the running marker and the database are gone), then performs exactly one fixed
// action from its own program directory: reopen the preparation page, or start the registered
// uninstaller of this installation with the fixed internal parameter.
internal static class UninstallHelper
{
    internal static int Run(string mode, int processId,string? profile=null)
    {
        try
        {
            using var waiting = Process.GetProcessById(processId);
            waiting.WaitForExit();
        }
        catch (ArgumentException)
        {
            // The process is already gone: the marker has been released for us to continue.
        }
        catch (Exception error) when (error is InvalidOperationException
            or System.ComponentModel.Win32Exception or NotSupportedException) { return 1; }
        var program = Path.GetDirectoryName(Environment.ProcessPath!)!;
        if (mode == UninstallRule.OpenPreparationHelper)
            return UninstallRule.StartPreparationPage(program,profile) ? 0 : 1;
        var uninstaller = UninstallRule.ResolveUninstaller(program, UninstallRule.RegisteredUninstaller);
        return uninstaller is not null && UninstallRule.StartRegisteredUninstaller(program, uninstaller) ? 0 : 1;
    }
}
