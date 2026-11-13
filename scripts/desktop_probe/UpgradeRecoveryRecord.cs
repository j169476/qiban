// SPDX-License-Identifier: MIT
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Qiban.DesktopProbe;

// What the maintenance window may offer, derived only from this machine's own upgrade record.
//
// That record is the authority on which program copy is the maintenance carrier, so the window
// takes the carrier path from it - never from an argument, a page or anything else a caller typed.
// A path a person supplied can therefore never become the program that overwrites the installed
// directory. This reader stays deliberately small: it answers "is there a rollback to offer" and
// "where is the carrier", while every real decision stays in the backend verbs, which re-validate
// the whole record and both published program identities before touching anything.
internal sealed class UpgradeRecoveryRecord
{
    // Set on the hand-over launch so a carrier can never hand over to itself in a loop.
    internal const string CarrierMarker = "QIBAN_UPGRADE_RECOVERY_CARRIER";
    static readonly Regex Digest = new("\\A[0-9a-f]{64}\\z", RegexOptions.CultureInvariant);
    static readonly Regex Identifier = new("\\A[0-9a-f]{32}\\z", RegexOptions.CultureInvariant);
    static readonly string[] Phases = { "prepared","staging","staged","rollback-ready",
        "rolling-back","confirmed","rolled-back","aborted","emergency","emergency-switching",
        "emergency-rolled-back" };

    UpgradeRecoveryRecord(string root,string installed,string? carrier,string transaction,
        string? phase,string? reason,string? normalBlocker,string? emergencyBlocker)
    {
        Root=root;Installed=installed;Carrier=carrier;Transaction=transaction;
        Phase=phase;Reason=reason;NormalBlocker=normalBlocker;EmergencyBlocker=emergencyBlocker;
    }

    internal string Root {get;}
    internal string Installed {get;}
    internal string? Carrier {get;}
    internal string Transaction {get;}
    internal string? Phase {get;}
    // Why nothing at all is offered; null means a rollback may be attempted.
    internal string? Reason {get;}
    // Why one of the two buttons is not offered while the other still is.
    internal string? NormalBlocker {get;}
    internal string? EmergencyBlocker {get;}
    internal bool CanRollback => Reason is null;
    // Only a switched-but-unprepared record still needs the new program to preserve its own data;
    // that first half is the one step the emergency branch may have to replace.
    internal bool Prepare => Phase is "staged" or "confirmed";

    internal bool IsCarrier
    {
        get
        {
            string? carrier=Carrier;
            return carrier is not null && Same(ProgramDirectory,carrier);
        }
    }

    internal static string ProgramDirectory
    {
        get
        {
            string? path = Environment.ProcessPath;
            return path is null ? "" : Full(Path.GetDirectoryName(path));
        }
    }

    // An ordinary rollback preserves in the newly installed program first, unless that half was
    // already recorded - then only the switch back remains, which is exactly how an interrupted
    // attempt is finished instead of started again. The emergency branch is one task: the backend
    // seals the faulty installation and switches back from the carrier behind a single verb.
    internal string[] Steps(bool emergency)
    {
        if(emergency)return new[]{ "emergency" };
        return Prepare ? new[]{ "rollback-prepare","rollback" } : new[]{ "rollback" };
    }

    internal static string Worker(string application)
        => Path.Combine(application,"resources","storage","Qiban.Storage.exe");

    // A carrier that is not this machine's own copy may never run the window: nothing here knows
    // a second program directory, so the reason is fixed and nothing is attempted.
    internal UpgradeRecoveryRecord Refuse(string reason)
        => new(Root,Installed,null,Transaction,Phase,reason,null,null);

    internal static UpgradeRecoveryRecord Read(string? root,string? installed)
    {
        string rootPath=Full(root), installedPath=Full(installed);
        if(rootPath.Length==0 || !Directory.Exists(rootPath)
            || !File.Exists(Path.Combine(rootPath,"installation.json")))
            return Unusable(rootPath,installedPath,"本机资料目录不可用，没有可回退的升级。");
        string journal=Path.Combine(rootPath,"upgrade","transaction.json");
        if(!File.Exists(journal))
            return Unusable(rootPath,installedPath,"这台电脑没有升级记录，不需要回退。");
        string transaction, phase, identity;
        try
        {
            using var document=JsonDocument.Parse(File.ReadAllText(journal));
            var value=document.RootElement;
            if(value.ValueKind!=JsonValueKind.Object)throw new JsonException();
            transaction=value.GetProperty("id").GetString()??"";
            phase=value.GetProperty("phase").GetString()??"";
            identity=value.GetProperty("carrier").GetProperty("identity_sha256").GetString()??"";
            if(value.GetProperty("format").GetInt32()!=1 || !Identifier.IsMatch(transaction)
                || Array.IndexOf(Phases,phase)<0 || !Digest.IsMatch(identity))throw new JsonException();
        }
        catch(Exception error) when(error is JsonException or IOException or UnauthorizedAccessException
            or KeyNotFoundException or InvalidOperationException or FormatException
            or System.Security.SecurityException)
        {
            return Unusable(rootPath,installedPath,"升级记录无法读取，本次未做任何改动。请保留资料目录并人工检查。");
        }
        string carrier=Path.Combine(rootPath,"upgrade","programs",identity);
        // The ordinary and the emergency branch never share a record: an open record carries either
        // rollback progress or an emergency archive, so at most one of them is still available.
        bool ordinary=phase is "staged" or "rollback-ready" or "rolling-back" or "confirmed";
        bool escalated=phase is "staged" or "confirmed" or "emergency" or "emergency-switching";
        if(!ordinary && !escalated)
        {
            string reason=phase switch
            {
                "rolled-back"=>"这次升级已经回退完成，不需要再次执行。",
                "emergency-rolled-back"=>"这次升级已经通过紧急回退回到旧版本，不需要再次执行。",
                // Nothing has been switched yet, so there is nothing to roll back: the step that is
                // still missing belongs to the installing package that prepared this transaction.
                // Opening 栖伴 again would not continue it, so that is not offered here.
                "prepared" or "staging"=>"这次升级还没有切换资料，暂时没有可回退的内容。"
                    +"请关闭本窗口，重新运行这次升级所用的同一个安装包，把准备工作做完；"
                    +"只重新打开栖伴不会继续这一步。",
                _=>"这次升级已经放弃，没有需要回退的内容。"
            };
            return new UpgradeRecoveryRecord(rootPath,installedPath,carrier,transaction,phase,reason,
                null,null);
        }
        if(!Directory.Exists(carrier) || !File.Exists(Worker(carrier)))
            return new UpgradeRecoveryRecord(rootPath,installedPath,carrier,transaction,phase,
                "维护载体缺少维护后台，无法执行回退。请保留资料目录并人工检查。",null,null);
        // The emergency branch runs entirely from the carrier, so a broken installation blocks only
        // the ordinary first step - and that is exactly the case emergency exists for.
        string? normal=!ordinary
            ? "这次升级已经进入紧急回退流程，常规回退不再可用，请继续使用紧急回退。"
            : ((phase is "staged" or "confirmed") && !File.Exists(Worker(installedPath))
                ? "新版维护后台不可用，无法执行常规回退。如果新版已经无法启动，请改用紧急回退。"
                : null);
        string? emergency=escalated
            ? null
            : "这次升级已经由新版本保全市资料，紧急回退不适用，请继续使用常规回退。";
        return new UpgradeRecoveryRecord(rootPath,installedPath,carrier,transaction,phase,null,
            normal,emergency);
    }

    static UpgradeRecoveryRecord Unusable(string root,string installed,string reason)
        => new(root,installed,null,"",null,reason,null,null);

    static string Full(string? value)
    {
        if(string.IsNullOrWhiteSpace(value))return "";
        try{return Path.TrimEndingDirectorySeparator(Path.GetFullPath(value));}
        catch(Exception error) when(error is ArgumentException or NotSupportedException
            or IOException or System.Security.SecurityException){return "";}
    }

    static bool Same(string left,string right)=>left.Length>0 && right.Length>0
        && string.Equals(left,right,StringComparison.OrdinalIgnoreCase);
}
