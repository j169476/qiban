// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// Offline checks for the uninstall preparation rules: which uninstaller this program may start,
// what the published alias forwards to, and that no backup result except a real success - or the
// person's own explicit decision to go on without one - ever unlocks continuing. Nothing here
// starts an uninstaller, touches an installed application or deletes a file.
internal static class UninstallTests
{
    internal static int Run(string directory)
    {
        directory=Path.GetFullPath(directory);
        Directory.CreateDirectory(directory);
        var cases = new List<string>();
        void Require(bool value, string message)
        {
            if (!value) throw new Exception(message);
        }
        UninstallSession Session(string program, string? uninstaller, bool backup = true) =>
            new(program, Path.Combine(directory, "profile"), uninstaller, backup,
                backup ? "" : "本机还没有已就绪的资料");
        try
        {
            // ---- the published alias, the helper modes and the fixed hand-off argument -----------
            Require(UninstallRule.IsAlias("uninstall.exe") && UninstallRule.IsAlias("UNINSTALL.EXE"),
                "alias name is not recognised");
            Require(!UninstallRule.IsAlias("Qiban.exe") && !UninstallRule.IsAlias(null),
                "the main executable must never act as the alias");
            Require(UninstallRule.AliasTarget(@"C:\Programs\Qiban") == @"C:\Programs\Qiban\Qiban.exe",
                "alias target is not the neighbouring shell");
            Require(UninstallRule.AliasArguments() is ["--uninstall"],
                "the alias must forward exactly one preparation argument");
            Require(UninstallRule.HandoffArguments() is ["/QIBANPREPARED=1"],
                "the registered uninstaller must receive the one fixed internal parameter");
            Require(UninstallRule.IsHelper("--open-uninstall") && UninstallRule.IsHelper("--finish-uninstall")
                    && !UninstallRule.IsHelper("--uninstall") && !UninstallRule.IsHelper("cmd.exe"),
                "only the two fixed helper modes are recognised");
            cases.Add("alias_and_helpers_are_fixed_and_name_no_command");

            // ---- resolving the registered uninstaller -------------------------------------------
            var program = Path.Combine(directory, "程序");
            Directory.CreateDirectory(program);
            var inside = Path.Combine(program, "unins000.exe");
            File.WriteAllBytes(inside, "fixture uninstaller\n"u8.ToArray());
            string? FakeRegistration() => inside;

            Require(UninstallRule.ResolveUninstaller(program, FakeRegistration) == inside,
                "a registered uninstaller inside the program directory must be used");
            Require(UninstallRule.ResolveUninstaller(program, () => null) is null,
                "a copied uninstaller without this installation's registration must not be used");
            var outside = Path.Combine(directory, "别的目录", "unins000.exe");
            Directory.CreateDirectory(Path.GetDirectoryName(outside)!);
            File.WriteAllBytes(outside, "decoys\n"u8.ToArray());
            var onlyOutside = Path.Combine(directory, "仅注册外部");
            Directory.CreateDirectory(onlyOutside);
            Require(UninstallRule.ResolveUninstaller(onlyOutside, () => outside) is null,
                "a registration pointing elsewhere must never be started");
            Require(!UninstallRule.StartRegisteredUninstaller(onlyOutside, outside) && File.Exists(outside),
                "an uninstaller outside the program directory must never be started");
            var sibling = Path.Combine(directory, "程序-其他", "unins000.exe");
            Directory.CreateDirectory(Path.GetDirectoryName(sibling)!);
            File.WriteAllBytes(sibling, "decoys\n"u8.ToArray());
            Require(!UninstallRule.IsInside(program, sibling) && UninstallRule.IsInside(program, inside),
                "a sibling directory with a similar name is not inside the program directory");
            foreach (var wrong in new[] { "unins0001.exe", "uninstall.exe", "other.exe", "uninsabc.exe" })
                Require(!UninstallRule.IsUninstallerName(wrong), $"accepted a non-uninstaller name: {wrong}");
            Require(UninstallRule.Unquoted("\"C:\\A B\\unins000.exe\" --mode") == @"C:\A B\unins000.exe"
                    && UninstallRule.Unquoted(@"C:\A\unins000.exe") == @"C:\A\unins000.exe",
                "a quoted or plain registration value must be read as one path");
            cases.Add("only_a_registered_uninstaller_inside_the_program_directory_resolves");

            // ---- a directory that was only unpacked ---------------------------------------------
            var portable = Path.Combine(directory, "免安装目录");
            Directory.CreateDirectory(portable);
            File.WriteAllBytes(Path.Combine(portable, "Qiban.exe"), "shell\n"u8.ToArray());
            File.WriteAllBytes(Path.Combine(portable, "uninstall.exe"), "shell\n"u8.ToArray());
            var portableSession = Session(portable, UninstallRule.ResolveUninstaller(portable, () => null));
            Require(!portableSession.Registered, "an unpacked directory must not resolve an uninstaller");
            portableSession.BackupSucceeded("本机备份.ccbackup");
            Require(!portableSession.CanContinue && portableSession.ContinueRefusal() == "uninstaller_missing",
                "continuing without a registered uninstaller must stay refused");
            Require(UninstallRule.PortableDirectoryGuidance.Contains("手动删除")
                    && UninstallRule.PortableDirectoryGuidance.Contains("备份与凭据"),
                "the unpacked-directory guidance must tell the person to delete it by hand");
            Require(!UninstallRule.StartRegisteredUninstaller(portable, Path.Combine(portable, "uninstall.exe"))
                    && File.Exists(Path.Combine(portable, "Qiban.exe"))
                    && File.Exists(Path.Combine(portable, "uninstall.exe")),
                "an unregistered or non-uninstaller entry must never be started");
            cases.Add("missing_uninstaller_is_described_and_never_removes_the_directory");

            // ---- a cancelled, refused or failed backup never unlocks continuing ------------------
            var prepared = Path.Combine(directory, "已安装");
            Directory.CreateDirectory(prepared);
            File.WriteAllBytes(Path.Combine(prepared, "unins000.exe"), "fixture uninstaller\n"u8.ToArray());
            var session = Session(prepared,
                UninstallRule.ResolveUninstaller(prepared, () => Path.Combine(prepared, "unins000.exe")));
            Require(session.Registered && !session.CanContinue
                    && session.ContinueRefusal() == "uninstall_backup_incomplete",
                "a fresh preparation must not offer continuing");
            session.BeginBackup();
            Require(session.Busy && session.ContinueRefusal() == "uninstall_backup_running",
                "a running backup must block continuing");
            session.BackupFailed("backup_failed", "本机备份没有完成");
            Require(session.Backup == UninstallBackupState.Failed && !session.CanContinue
                    && session.BackupDetail == "本机备份没有完成",
                "a failed backup must leave the page in place");
            Require(!session.HandedOff, "a failed backup must never start the uninstaller");
            session.BeginBackup();
            session.BackupFailed("cancelled", "已取消保存备份，资料未改变。");
            Require(!session.CanContinue && session.BackupName == "",
                "a cancelled backup must not be remembered as a success");
            cases.Add("cancelled_or_failed_backup_never_unlocks_continuing");

            // ---- only a real success, or the person's own skip, allows continuing ---------------
            session.BeginBackup();
            session.BackupSucceeded("qiban-20261202.ccbackup");
            Require(session.CanContinue && session.Backup == UninstallBackupState.Succeeded
                    && session.BackupName == "qiban-20261202.ccbackup" && session.ContinueRefusal() == "",
                "a successful backup is what unlocks continuing");
            Require(!session.HandedOff, "a successful backup alone must not start the uninstaller");
            session.MarkHandedOff();
            Require(session.HandedOff && session.ContinueRefusal() == "uninstall_already_started",
                "one preparation must hand off at most once");
            var skipping = Session(prepared, Path.Combine(prepared, "unins000.exe"));
            skipping.SkipBackup();
            Require(skipping.CanContinue && skipping.Backup == UninstallBackupState.Skipped
                    && skipping.BackupName == "",
                "the explicit skip is the only other way to continue");
            var noData = Session(prepared, Path.Combine(prepared, "unins000.exe"), backup: false);
            Require(!noData.BackupAvailable && noData.BackupUnavailableReason != ""
                    && !noData.CanContinue,
                "a profile without local data must not offer a backup that would create a database");
            cases.Add("success_or_an_explicit_skip_is_required_before_continuing");
            cases.Add("preparation_without_local_data_offers_no_backup");

            Require(UninstallRule.HandoffArguments().Length==1,"default uninstall must retain profile");
            Require(UninstallRule.HandoffArguments(true).Contains("/QIBANCLEARPROFILE=1"),"clear choice lost");
            Require(UninstallRule.IsHelper(UninstallRule.FinishClearHelper),"clear helper missing");
            Require(!ProfileCleanup.IsDefault(Path.Combine(directory,"profile")),"custom profile must not be cleared");
            string synthetic=Path.Combine(directory,"cleanup-"+Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(synthetic,"data"));
            File.WriteAllText(Path.Combine(synthetic,"data","synthetic.txt"),"test data");
            string externalBackup=Path.Combine(directory,"external-backup.txt");
            File.WriteAllText(externalBackup,"external backup");
            ProfileCleanup.DeleteTree(synthetic,directory);
            Require(!Directory.Exists(synthetic)&&File.Exists(externalBackup),"cleanup escaped profile");
            bool refused=false;
            try{ProfileCleanup.DeleteTree(directory,directory);}catch(IOException){refused=true;}
            Require(refused,"invalid cleanup boundary accepted");
            cases.Add("explicit_cleanup_removes_only_synthetic_profile_and_keeps_external_backup");

            File.WriteAllText(Path.Combine(directory, "uninstall-result.json"),
                JsonSerializer.Serialize(new { passed = true, cases }));
            return 0;
        }
        catch (Exception error)
        {
            File.WriteAllText(Path.Combine(directory, "uninstall-result.json"),
                JsonSerializer.Serialize(new { passed = false, cases, error = error.Message }));
            return 1;
        }
    }
}
