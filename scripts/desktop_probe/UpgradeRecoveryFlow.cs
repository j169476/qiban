// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// What one maintenance task did, in the words this window may show.
internal sealed record UpgradeRecoveryOutcome(bool Success,bool Interrupted,string Message,
    string? Display,int Executed);

// One Windows confirmation covers one whole rollback task: the verbs behind it are internal steps
// and never ask again. The sequence comes from the record, so the same flow runs the ordinary path
// (preserve in the new program, switch back from the carrier), a resumed switch back after an
// interrupted attempt, and the D62 emergency path. It is built from delegates so a test can drive
// it without a window, a backend or a Windows prompt.
internal sealed class UpgradeRecoveryFlow(Func<CancellationToken,Task<string>> verify,
    Func<string,CancellationToken,Task<UpgradeHostReply>> execute,Action<string>? progress=null)
{
    // A second press while one task runs must do nothing at all, not start another rollback.
    static readonly SemaphoreSlim Gate=new(1,1);
    internal const string Unknown="回退没有完成，本次结果不确定。请重新打开本窗口检查升级状态，不要重复执行。";
    internal const string Busy="另一个回退操作正在进行，本次未执行。";

    internal async Task<UpgradeRecoveryOutcome> Run(bool emergency,string[] steps,CancellationToken token)
    {
        if(!await Gate.WaitAsync(0))return new UpgradeRecoveryOutcome(false,false,Busy,null,0);
        int executed=0;
        string? display=null;
        bool retained=false;
        try
        {
            string verified;
            try{verified=await verify(token);}
            catch(OperationCanceledException){verified="Canceled";}
            if(verified!="Verified")
                return new UpgradeRecoveryOutcome(false,false,WindowsVerification.UpgradeMessage(verified),
                    null,0);
            foreach(var step in steps)
            {
                token.ThrowIfCancellationRequested();
                progress?.Invoke(StepText(step));
                executed++;
                var reply=await execute(step,token);
                display=reply.Sealed??reply.Preservation??display;
                retained|=reply.Retained;
                if(!reply.Ok)
                {
                    var (message,unknown)=Explain(reply.Error);
                    return new UpgradeRecoveryOutcome(false,unknown,message,display,executed);
                }
            }
            return new UpgradeRecoveryOutcome(true,false,Success(emergency,display,retained),display,executed);
        }
        catch(OperationCanceledException){return new UpgradeRecoveryOutcome(false,true,Unknown,display,executed);}
        catch(Exception error) when(error is IOException or System.ComponentModel.Win32Exception
            or JsonException or InvalidOperationException or TimeoutException
            or System.Security.SecurityException)
        {
            // A maintenance step that could not even be carried out is an unknown result, never a
            // failure the window may present as "nothing changed".
            return new UpgradeRecoveryOutcome(false,true,Unknown,display,executed);
        }
        finally{Gate.Release();}
    }

    // Every backend code becomes one fixed sentence here: the page, the window and the log never
    // see backend text, a path or an exception message. Only a code that is certainly decided before
    // any write claims "nothing was changed"; every code that can also appear after the pointer
    // moved or after files were half-written is one uncertain result with one instruction - reopen
    // this window and check the state, never repeat the task.
    internal static (string Message,bool Unknown) Explain(string? code)=>code switch
    {
        "upgrade_running"=>("栖伴仍在运行。请先在栖伴窗口中正常退出，再回到本窗口重试。",false),
        "upgrade_busy"=>("另一个升级维护操作正在进行，本次未执行。请稍后再试。",false),
        "upgrade_lock" or "upgrade_directory_permissions"=>
            ("本机资料目录不可写，本次未做改动。请检查目录权限后重试。",false),
        "upgrade_root"=>("本机资料目录不可用，未继续维护操作。",false),
        "upgrade_journal_missing"=>("没有找到这次升级的记录，无法回退。",false),
        "upgrade_journal_invalid"=>("升级记录无法读取，本次未做改动。请保留资料目录并人工检查。",false),
        "upgrade_executing_program"=>("回退没有从维护载体运行，本次没有回退。",false),
        "upgrade_rollback_program"=>
            ("只有这次升级装入的新版本才能保全当前资料。如果新版已经无法启动，请改用紧急回退。",false),
        "upgrade_database" or "upgrade_database_busy" or "upgrade_database_failed"=>
            ("本机数据库无法由维护流程使用，本次未做改动。请确认栖伴已经退出后重试。",false),
        "upgrade_verification_invalid" or "upgrade_verification_expired"=>
            ("这次 Windows 验证结果无效或已过期，没有执行回退。",false),
        "upgrade_host_required" or "upgrade_rollback_unavailable"
            or "upgrade_host_parent" or "upgrade_host_receipt" or "upgrade_host_missing"=>
            ("维护后台没有接受这次回退请求，本次未执行。请使用包含回退入口的完整安装包后重试。",false),
        "upgrade_host_start"=>("维护窗口没能启动维护后台，本次没有执行任何操作。",false),
        "upgrade_emergency_running"=>
            ("本机数据库仍在运行，请先在栖伴窗口中正常退出后再继续紧急回退。",false),
        "upgrade_emergency_stopped"=>
            ("无法确认本机数据库已经停止，本次未做紧急封存，程序与资料未改动。",false),
        "upgrade_emergency_space"=>
            ("磁盘剩余空间不足以封存本机资料，本次未做紧急封存，程序与资料未改动。",false),
        "upgrade_emergency_vault" or "upgrade_emergency_credentials"=>
            ("本机凭据库或用户加密不可用，本次未做紧急封存，程序与资料未改动。",false),
        "upgrade_emergency_path"=>
            ("本机资料目录中存在链接或特殊条目，本次未做紧急封存，请人工检查。",false),
        "upgrade_emergency_copy" or "upgrade_emergency_write" or "upgrade_emergency_permissions"=>
            ("紧急封存没有写出完整副本，本次未切换程序与资料。重试会重新建立封存。",false),
        // These can occur with the pointer already switched or files half-written: no safe claim is
        // made beyond "the task did not finish - check the state before doing anything else".
        "upgrade_state" or "upgrade_write" or "upgrade_stop_failed" or "upgrade_emergency_state"
            or "upgrade_program_missing" or "upgrade_program_invalid" or "upgrade_program_read"
            or "upgrade_program_changed"=>(Unknown,true),
        _=>(Unknown,true)
    };

    static string Success(bool emergency,string? display,bool retained)
    {
        // The ordinary way back keeps this machine's newest settings - only the emergency branch
        // returns to the older generation's own settings, and the two sentences must not be mixed.
        string notice=emergency
            ?"紧急回退已完成：升级后的内容暂时不会出现，应用设置回到升级前的状态，故障资料已封存。"
            :"回退已完成：程序和资料回到升级前的状态，升级后的资料已保全，本机设置保持升级后的最新状态。";
        string saved=display is null?"":(emergency?"\n封存记录：":"\n保全文件：")+display;
        // The emergency way back cannot always know the newer build's complete file set, and it
        // never deletes a file nobody published: that risk is named here instead of being hidden.
        string kept=retained
            ?"\n注意：程序目录里还有新版本留下的文件没有被删除。如果重新打开栖伴失败，请人工检查程序目录。"
            :"";
        return notice+saved+kept+"\n请重新打开栖伴；所有平台保持停用，需要时请在应用内重新启用。";
    }

    static string StepText(string step)=>step switch
    {
        "rollback-prepare"=>"正在由新版本保全升级后的资料……",
        "rollback"=>"正在切回升级前的资料与程序……",
        _=>"正在封存升级后的故障资料并切回升级前的资料……"
    };
}
