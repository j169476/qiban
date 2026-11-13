// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;

namespace Qiban.DesktopProbe;
internal static class GroupTests
{
    static void Require(bool value,string message){if(!value)throw new Exception(message);}
    internal static async Task<int> Run(string directory)
    {
        Directory.CreateDirectory(directory);var results=new List<object>();
        try
        {
            foreach(var scenario in new[]{"normal","dependency-fails","dependent-fails","cancel","dependent-crash",
                "dependency-crash","fail-stop","ignore-stop","mixed-stop","wait-force"})
            {
                var actions=new List<string>();var processes=new List<Process>();
                ComponentDefinition Definition(string id,string mode)
                {
                    var definition=ProbeService.Definition("qiban-group-"+Guid.NewGuid().ToString("N"),mode,stopMs:500);
                    return definition with {Id=id,WaitForExplicitForce=scenario=="wait-force" && id=="b",Launch=()=>
                    {
                        actions.Add("start-"+id);var owned=definition.Launch();
                        var observed=Process.GetProcessById(owned.Process.Id);_=observed.Handle;processes.Add(observed);return owned;
                    },RequestStop=async(p,t)=>{actions.Add("stop-"+id);await definition.RequestStop(p,t);}};
                }
                using var group=new ComponentGroup(Definition("a",scenario=="dependency-fails"?"version":scenario=="mixed-stop"?"ignore-stop":"normal"),
                    Definition("b",scenario=="dependent-fails"?"version":scenario=="cancel"?"never-ready":
                        scenario is "fail-stop" or "mixed-stop"?"fail-stop":scenario is "ignore-stop" or "wait-force"?"ignore-stop":"normal"));
                try
                {
                    var start=group.StartAsync();
                    if(scenario=="cancel")
                    {
                        var deadline=Stopwatch.StartNew();
                        while(group.Process is null){Require(deadline.Elapsed.TotalSeconds<8,"Dependent never started");await Task.Delay(10);}
                        var queued=group.StartAsync();var stop=group.StopAsync();await Task.WhenAll(start,queued,stop);
                        Require(group.State=="stopped"&&actions.Count(a=>a.StartsWith("start"))==2,"Stop did not cancel queued group start");
                    }
                    else
                    {
                        await start;
                        if(scenario=="wait-force")
                        {
                            var stop=group.StopAsync();await Task.Delay(800);
                            Require(!stop.IsCompleted && processes.All(p=>!p.HasExited),"Group did not wait without killing prerequisites");
                            group.RequestForceStop();await stop.WaitAsync(TimeSpan.FromSeconds(5));
                            Require(group.Detail=="forced_stop" && processes.All(p=>p.HasExited),"Explicit force did not stop all owned members");
                        }
                        else if(scenario=="dependency-fails")Require(group.State=="failed"&&actions.SequenceEqual(new[]{"start-a"}),"Dependent ran without prerequisite");
                        else if(scenario=="dependent-fails")Require(group.State=="failed"&&actions.SequenceEqual(new[]{"start-a","start-b","stop-a"}),"Failed startup did not stop prerequisite");
                        else if(scenario is "dependent-crash" or "dependency-crash")
                        {
                            var crashed=processes[scenario=="dependent-crash"?1:0];
                            crashed.Kill();await crashed.WaitForExitAsync();await group.CheckAsync();
                            Require(group.State=="failed"&&processes.All(p=>p.HasExited),"Group was not stopped after member failure");
                            await group.StopAsync();await group.StopAsync();
                            Require(group.Detail=="stop_failed"&&processes.Count==2,"Repeated stop erased crash or restarted a member");
                            await group.StartAsync();Require(group.State=="ready","Explicit recovery failed");
                            await group.StopAsync();Require(group.Detail=="graceful_stop","New healthy run retained old failure");
                        }
                        else if(scenario is "fail-stop" or "ignore-stop" or "mixed-stop")
                        {
                            await group.StopAsync();
                            string expected=scenario=="ignore-stop"?"forced_stop":"stop_failed";
                            Require(group.Detail==expected,"Group termination severity lost");
                            await group.StopAsync();
                            Require(group.Detail==expected&&processes.Count==2,"Repeated stop changed termination outcome");
                        }
                        else
                        {
                            await Task.WhenAll(group.StartAsync(),group.StartAsync());
                            await group.StopAsync();
                            Require(actions.SequenceEqual(new[]{"start-a","start-b","stop-b","stop-a"}),"Incorrect dependency order or duplicate starts");
                        }
                    }
                    Require(processes.All(p=>p.HasExited),"Group left a process alive before test cleanup");
                    results.Add(new{scenario,passed=true,actions=actions.ToArray()});
                }
                finally{if(scenario=="wait-force")group.RequestForceStop();await group.StopAsync();foreach(var process in processes)process.Dispose();}
            }
            File.WriteAllText(Path.Combine(directory,"group-result.json"),JsonSerializer.Serialize(new{passed=true,results}));return 0;
        }
        catch(Exception error){File.WriteAllText(Path.Combine(directory,"group-result.json"),JsonSerializer.Serialize(new{passed=false,results,error=error.ToString()}));return 1;}
    }
}
