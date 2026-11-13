// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;

namespace Qiban.DesktopProbe;

// The upgrade-confirm member's own contract: which answer lets a start continue, which code a
// refusal carries, that it sits between the database and the backend, and that an ordinary start
// still runs through all four members in order.
internal static class UpgradeConfirmTests
{
    static void Require(bool value,string message){if(!value)throw new Exception(message);}

    internal static async Task<int> Run(string directory)
    {
        Directory.CreateDirectory(directory);var results=new List<object>();
        // Written after every case: a case that cannot finish left the earlier ones on record.
        void Save(bool passed,string? error=null)
            =>File.WriteAllText(Path.Combine(directory,"upgrade-confirm-result.json"),
                JsonSerializer.Serialize(new{passed,results,error}));
        try
        {
            results.Add(Verdicts());Save(false);
            results.Add(Order(directory));Save(false);
            results.Add(await Sequence(directory,"refused",false));Save(false);
            results.Add(await Sequence(directory,"ready",true));Save(false);
            Save(true);
            return 0;
        }
        catch(Exception error)
        {
            try{Save(false,error.ToString());}catch(IOException){}
            return 1;
        }
    }

    // A start with nothing to confirm, and a confirmed one that left cleanup work behind, are both
    // ready answers; a refusal keeps its stable code and backend text never becomes one.
    static object Verdicts()
    {
        var verdicts=new List<object>();
        foreach(var answer in new[]
        {
            "{\"ready\":true,\"action\":\"none\",\"phase\":null,\"warning\":null}",
            "{\"ready\":true,\"action\":\"none\",\"phase\":\"confirmed\",\"warning\":null}",
            "{\"ready\":true,\"action\":\"confirm\",\"phase\":\"confirmed\",\"warning\":{\"code\":\"upgrade_cleanup_incomplete\",\"failed\":[{\"generation\":\"d\",\"code\":\"upgrade_preserve\"}]}}"
        })
        {
            Require(UpgradeConfirmComponent.Outcome(answer) is null,"A ready answer stopped the start: "+answer);
            verdicts.Add(new{answer,code=(string?)null});
        }
        foreach(var (answer,code) in new[]
        {
            ("{\"ok\":false,\"error\":\"upgrade_maintenance_in_progress\"}","upgrade_maintenance_in_progress"),
            ("{\"ok\":false,\"error\":\"upgrade_rollback_pending\"}","upgrade_rollback_pending"),
            ("{\"ok\":false,\"error\":\"upgrade_confirm_program\"}","upgrade_confirm_program")
        })
        {
            Require(UpgradeConfirmComponent.Outcome(answer)==code,"Refusal code lost: "+answer);
            verdicts.Add(new{answer,code});
        }
        foreach(var answer in new[]
        {
            "not json at all","","{}","{\"phase\":\"staged\"}","{\"ready\":false}",
            "{\"ok\":false,\"error\":\"Traceback: password=hunter2 C:\\\\Users\\\\x\"}",
            "{\"ok\":false,\"error\":\"UPGRADE_STATE\"}"
        })
        {
            Require(UpgradeConfirmComponent.Outcome(answer)=="upgrade_startup_failed","An unusable answer was trusted: "+answer);
            verdicts.Add(new{answer,code="upgrade_startup_failed"});
        }
        return new{casetype="verdicts",passed=true,cases=verdicts};
    }

    // The production wiring, without starting anything: the decision runs after the database and
    // before ordinary business, which is the only place that makes a refusal harmless.
    static object Order(string directory)
    {
        string pipe="qiban-confirm-order-"+Guid.NewGuid().ToString("N");
        using var group=InstallationComponent.Group(directory,AppContext.BaseDirectory,pipe);
        var ids=JsonSerializer.SerializeToElement(group.Snapshot).EnumerateArray()
            .Select(member=>member.GetProperty("id").GetString()).ToArray();
        Require(ids.SequenceEqual(new[]{"installation","postgresql","upgrade-confirm","encrypted-storage"}),
            "Upgrade confirmation is not between the database and the backend: "+string.Join(",",ids));
        return new{casetype="order",passed=true,members=ids};
    }

    // One real group with the production member order. The confirming member keeps its real
    // definition (identity, timeouts and exit reason); only the child it launches is replaced, so a
    // refused confirmation must stop the database and must never start the backend.
    static async Task<object> Sequence(string directory,string outcome,bool ready)
    {
        var actions=new List<string>();var processes=new List<Process>();
        var members=new List<ComponentDefinition>();
        foreach(var (id,mode) in new[]{("installation","normal"),("postgresql","normal"),
            ("upgrade-confirm",outcome=="refused"?"exit":"normal"),("encrypted-storage","normal")})
        {
            // One pipe per member, shared by the definition that is ready-checked and the child
            // that answers it - the same contract the production definitions have.
            string pipeName="qiban-confirm-"+id+"-"+Guid.NewGuid().ToString("N");
            var probe=ProbeService.Definition(pipeName,mode,stopMs:500);
            ComponentDefinition definition=id=="upgrade-confirm"
                ? UpgradeConfirmComponent.Definition(directory,pipeName)
                : probe with{Id=id};
            var launch=probe.Launch;var stop=probe.RequestStop;
            members.Add(definition with
            {
                Launch=()=>
                {
                    actions.Add("start-"+id);var owned=launch();
                    var observed=Process.GetProcessById(owned.Process.Id);_=observed.Handle;processes.Add(observed);
                    return owned;
                },
                RequestStop=async(process,token)=>{actions.Add("stop-"+id);await stop(process,token);}
            });
        }
        using var group=new ComponentGroup(members.ToArray());
        try
        {
            await group.StartAsync();
            if(ready)
            {
                Require(group.State=="ready","Ordinary start did not become ready: "+group.Detail);
                Require(actions.SequenceEqual(new[]{"start-installation","start-postgresql",
                    "start-upgrade-confirm","start-encrypted-storage"}),"Start order changed: "+string.Join(",",actions));
                await group.StopAsync();
                Require(group.Detail=="graceful_stop","Ordinary start did not stop gracefully: "+group.Detail);
            }
            else
            {
                Require(group.State=="failed","Refused confirmation did not stop the group: "+group.State);
                Require(group.Detail=="upgrade_startup_failed","Wrong refusal code: "+group.Detail);
                Require(!actions.Contains("start-encrypted-storage"),"Business started after a refused confirmation");
                Require(actions.SequenceEqual(new[]{"start-installation","start-postgresql",
                    "start-upgrade-confirm","stop-postgresql","stop-installation"}),
                    "Refused confirmation did not stop what it had started: "+string.Join(",",actions));
                Require(processes.All(process=>process.HasExited),"Refused start left a process alive");
            }
            return new{casetype="sequence",outcome,passed=true,actions=actions.ToArray(),state=group.State,detail=group.Detail};
        }
        finally
        {
            foreach(var process in processes)process.Dispose();
        }
    }
}
