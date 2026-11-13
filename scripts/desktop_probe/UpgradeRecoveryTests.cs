// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// The rollback window's own contract, without a window and without a Windows prompt: which verbs one
// task runs, that a cancelled verification runs none, that a failure is never reported as success,
// that an interrupted task is an unknown result, and that a second press starts no second task.
internal static class UpgradeRecoveryTests
{
    static readonly string[] PrepareThenRollback={"rollback-prepare","rollback"};
    static readonly string[] RollbackOnly={"rollback"};
    static readonly string[] EmergencyOnly={"emergency"};

    static void Require(bool value,string message){if(!value)throw new Exception(message);}

    internal static async Task<int> Run(string directory)
    {
        Directory.CreateDirectory(directory);
        var results=new List<object>();
        // Written after every case: a case that cannot finish leaves the earlier ones on record.
        void Save(bool passed,string? error=null)=>File.WriteAllText(
            Path.Combine(directory,"upgrade-recovery-result.json"),
            JsonSerializer.Serialize(new{passed,results,error}));
        try
        {
            results.Add(Reads(directory));Save(false);
            results.Add(await Cancelled());Save(false);
            results.Add(await Normal());Save(false);
            results.Add(await Emergency());Save(false);
            results.Add(await Failed());Save(false);
            results.Add(await Once());Save(false);
            Save(true);
            return 0;
        }
        catch(Exception error)
        {
            try{Save(false,error.ToString());}catch(IOException){}
            return 1;
        }
    }

    // The carrier comes out of this machine's own record, the offered verbs follow the recorded
    // phase, and every phase that offers nothing says why.
    static object Reads(string directory)
    {
        string root=Path.GetFullPath(Path.Combine(directory,"record-root"));
        string installed=Path.Combine(root,"installed");
        string identity=new('a',64);
        string carrier=Path.Combine(root,"upgrade","programs",identity);
        Directory.CreateDirectory(Path.Combine(carrier,"resources","storage"));
        File.WriteAllText(Path.Combine(carrier,"resources","storage","Qiban.Storage.exe"),"backend");
        Directory.CreateDirectory(Path.Combine(installed,"resources","storage"));
        File.WriteAllText(Path.Combine(installed,"resources","storage","Qiban.Storage.exe"),"backend");
        File.WriteAllText(Path.Combine(root,"installation.json"),"{}");
        string journal=Path.Combine(root,"upgrade","transaction.json");
        var cases=new List<object>();
        foreach(var (phase,ordinary,escalated,prepare) in new[]
        {
            ("staged",true,true,true),("confirmed",true,true,true),
            ("rollback-ready",true,false,false),("rolling-back",true,false,false),
            ("emergency",false,true,false),("emergency-switching",false,true,false),
            ("rolled-back",false,false,false),("emergency-rolled-back",false,false,false),
            ("prepared",false,false,false),("aborted",false,false,false)
        })
        {
            Write(journal,phase,identity);
            var record=UpgradeRecoveryRecord.Read(root,installed);
            Require(record.CanRollback==(ordinary||escalated),
                "Phase "+phase+" was offered wrongly: "+record.Reason);
            if(record.CanRollback)
            {
                Require(string.Equals(record.Carrier,carrier,StringComparison.OrdinalIgnoreCase),
                    "The carrier path was not taken from the record for "+phase);
                Require((record.NormalBlocker is null)==ordinary,
                    "The ordinary path is offered wrongly for "+phase);
                Require((record.EmergencyBlocker is null)==escalated,
                    "The emergency path is offered wrongly for "+phase);
                if(ordinary)
                    Require(record.Steps(false).SequenceEqual(prepare?PrepareThenRollback:RollbackOnly),
                        "The ordinary steps are wrong for "+phase);
                if(escalated)Require(record.Steps(true).SequenceEqual(EmergencyOnly),"Emergency is not one verb");
            }
            else Require(!string.IsNullOrEmpty(record.Reason),"A phase that offers nothing has no reason");
            cases.Add(new{phase,ordinary,escalated,can_rollback=record.CanRollback,reason=record.Reason});
        }
        // A missing new backend blocks only the ordinary path: emergency runs from the carrier.
        File.Delete(Path.Combine(installed,"resources","storage","Qiban.Storage.exe"));
        Write(journal,"staged",identity);
        var blocked=UpgradeRecoveryRecord.Read(root,installed);
        Require(blocked.CanRollback && blocked.NormalBlocker is not null && blocked.EmergencyBlocker is null,
            "A missing new backend did not block exactly the ordinary path");
        // A carrier without a backend offers nothing at all: neither path could run.
        File.Delete(Path.Combine(carrier,"resources","storage","Qiban.Storage.exe"));
        var gone=UpgradeRecoveryRecord.Read(root,installed);
        Require(!gone.CanRollback && gone.Reason is not null,"A carrier without a backend offered a rollback");
        // No record, an unreadable record and a directory that is no installation root each explain
        // themselves instead of leaving the person in front of two dead buttons.
        File.Delete(journal);
        Require(!UpgradeRecoveryRecord.Read(root,installed).CanRollback,
            "A root without a record offered a rollback");
        File.WriteAllText(journal,"not json");
        Require(!UpgradeRecoveryRecord.Read(root,installed).CanRollback,
            "An unreadable record offered a rollback");
        Require(!UpgradeRecoveryRecord.Read(Path.Combine(directory,"empty"),installed).CanRollback,
            "A directory that is not an installation root offered a rollback");
        return new{casetype="record",passed=true,cases,blocked=blocked.NormalBlocker};
    }

    static void Write(string journal,string phase,string identity)=>File.WriteAllText(journal,
        JsonSerializer.Serialize(new{format=1,id=new string('b',32),installation=new string('c',32),
            phase,carrier=new{identity_sha256=identity}}));

    // A cancelled Windows verification runs no verb at all and still says what happened.
    static async Task<object> Cancelled()
    {
        var calls=new List<string>();
        var flow=new UpgradeRecoveryFlow(_=>Task.FromResult("Canceled"),(verb,_)=>
        {
            calls.Add(verb);
            return Task.FromResult(new UpgradeHostReply(true,null,null,null,null));
        });
        var outcome=await flow.Run(false,PrepareThenRollback,CancellationToken.None);
        Require(!outcome.Success && !outcome.Interrupted,"A cancelled verification looked like a result");
        Require(outcome.Executed==0 && calls.Count==0,"A cancelled verification still ran a verb");
        Require(outcome.Message.Contains("取消"),"The cancelled verification has no feedback");
        return new{casetype="cancelled",passed=true,message=outcome.Message};
    }

    // The ordinary path preserves first and switches back second, once each.
    static async Task<object> Normal()
    {
        var calls=new List<string>();
        var flow=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(verb,_)=>
        {
            calls.Add(verb);
            return Task.FromResult(new UpgradeHostReply(true,null,"rolled-back",null,null));
        });
        var outcome=await flow.Run(false,PrepareThenRollback,CancellationToken.None);
        Require(outcome.Success && outcome.Executed==2,"The ordinary rollback did not run both steps");
        Require(calls.SequenceEqual(PrepareThenRollback),"Wrong ordinary order: "+string.Join(",",calls));
        Require(outcome.Message.Contains("重新打开栖伴"),"The ordinary success does not ask to reopen");
        Require(outcome.Message.Contains("停用"),"The ordinary success does not say the platforms stay off");
        return new{casetype="ordinary",passed=true,calls,message=outcome.Message};
    }

    // The emergency path is exactly one verb, it carries the sealed copy's display name, and the
    // files it could not remove are named instead of being hidden.
    static async Task<object> Emergency()
    {
        var calls=new List<string>();
        var flow=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(verb,_)=>
        {
            calls.Add(verb);
            return Task.FromResult(new UpgradeHostReply(true,null,"emergency-rolled-back",null,
                "f41c0b6a1d2e3f405162738495a6b7c8"));
        });
        var outcome=await flow.Run(true,EmergencyOnly,CancellationToken.None);
        Require(outcome.Success && outcome.Executed==1,"The emergency path did not run exactly one verb");
        Require(calls.SequenceEqual(EmergencyOnly),"The emergency path ran the wrong verbs: "
            +string.Join(",",calls));
        Require(outcome.Display=="f41c0b6a1d2e3f405162738495a6b7c8","The sealed display name was lost");
        Require(outcome.Message.Contains("暂时不会出现")&&outcome.Message.Contains("封存记录"),
            "The emergency result does not explain itself");
        Require(!outcome.Message.Contains("程序目录里还有"),"An unnecessary warning was shown");
        var kept=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(_,_)=>Task.FromResult(
            new UpgradeHostReply(true,null,"emergency-rolled-back",null,null,true)));
        var warned=await kept.Run(true,EmergencyOnly,CancellationToken.None);
        Require(warned.Success && warned.Message.Contains("程序目录里还有"),
            "Files the emergency way back could not remove were not announced");
        return new{casetype="emergency",passed=true,calls,display=outcome.Display,
            retained=warned.Message};
    }

    // A failure is never reported as success, an interrupted step is an uncertain result that must
    // not be retried automatically, and a code that can appear after the pointer moved or after
    // files were half-written never claims that nothing happened.
    static async Task<object> Failed()
    {
        var calls=new List<string>();
        var refused=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(verb,_)=>
        {
            calls.Add(verb);
            return Task.FromResult(UpgradeHostReply.Failed("upgrade_running"));
        });
        var stopped=await refused.Run(false,PrepareThenRollback,CancellationToken.None);
        Require(!stopped.Success && !stopped.Interrupted && stopped.Executed==1,
            "A refused first step was not reported as nothing done");
        Require(calls.Count==1 && calls[0]=="rollback-prepare","A refused first step still switched back");
        Require(stopped.Message.Contains("仍在运行"),"A refusal does not explain itself");
        var half=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(verb,_)=>Task.FromResult(
            verb=="rollback-prepare"
                ?new UpgradeHostReply(true,null,"rollback-ready",null,null)
                :UpgradeHostReply.Failed("upgrade_state")));
        var partial=await half.Run(false,PrepareThenRollback,CancellationToken.None);
        Require(!partial.Success && partial.Executed==2,"A failed switch back was not reported as a failure");
        Require(!partial.Message.Contains("已完成"),"A failed switch back was reported as success");
        // A half-written program directory is an uncertain result, not "nothing was changed".
        var written=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(_,_)=>Task.FromResult(
            UpgradeHostReply.Failed("upgrade_program_changed")));
        var unclear=await written.Run(false,RollbackOnly,CancellationToken.None);
        Require(!unclear.Success && unclear.Interrupted && unclear.Message.Contains("不要重复执行"),
            "A code that may follow a half-written switch claimed nothing happened");
        using var cancelling=new CancellationTokenSource();
        var interrupted=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),(verb,token)=>
        {
            cancelling.Cancel();
            return Task.FromException<UpgradeHostReply>(new OperationCanceledException(token));
        });
        var unknown=await interrupted.Run(true,EmergencyOnly,cancelling.Token);
        Require(!unknown.Success && unknown.Interrupted,"An interrupted task looked like a success");
        Require(unknown.Message.Contains("不要重复执行"),"An interrupted task does not tell what to do");
        return new{casetype="failures",passed=true,stopped=stopped.Message,partial=partial.Message,
            unclear=unclear.Message,unknown=unknown.Message};
    }

    // While one task runs, a second press must not start another: it is refused, and the first task
    // still finishes its one switch back.
    static async Task<object> Once()
    {
        var calls=new List<string>();
        var release=new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        async Task<UpgradeHostReply> Hold(string verb,CancellationToken token)
        {
            calls.Add(verb);
            await release.Task;
            return new UpgradeHostReply(true,null,"rolled-back",null,null);
        }
        var flow=new UpgradeRecoveryFlow(_=>Task.FromResult("Verified"),Hold);
        var first=flow.Run(false,RollbackOnly,CancellationToken.None);
        var second=await flow.Run(false,RollbackOnly,CancellationToken.None);
        Require(!second.Success && second.Executed==0 && second.Message.Contains("正在进行"),
            "A second press started another task");
        release.SetResult();
        var done=await first;
        Require(done.Success && done.Executed==1 && calls.SequenceEqual(RollbackOnly),
            "The first task did not finish exactly one verb: "+string.Join(",",calls));
        return new{casetype="once",passed=true,calls,second=second.Message};
    }
}
