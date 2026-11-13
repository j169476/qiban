// SPDX-License-Identifier: MIT
using System.Diagnostics;

namespace Qiban.DesktopProbe;

// Definitions are supplied in dependency order by trusted application code.
internal sealed class ComponentGroup : IDisposable
{
    readonly ManagedComponent[] components;
    readonly SemaphoreSlim operations = new(1,1);
    readonly object intent = new();
    ManagedComponent? starting;
    long stopEpoch;
    internal string State {get; private set;} = "stopped";
    internal string? Detail {get; private set;}
    internal string Id => components.Length==1 ? components[0].Id : "application";
    internal Process? Process => components[^1].Process;
    internal object[] Snapshot => components.Select(c=>(object)new {id=c.Id,state=c.State,detail=c.Detail,health=c.HealthDiagnostic}).ToArray();
    internal event Action? Changed;
    internal ComponentGroup(params ComponentDefinition[] definitions)
    {
        if(definitions.Length==0 || definitions.Select(d=>d.Id).Distinct().Count()!=definitions.Length)
            throw new ArgumentException("Component identities must be nonempty and unique");
        components=definitions.Select(d=>new ManagedComponent(d)).ToArray();
        foreach(var component in components) component.Changed+=()=>Changed?.Invoke();
    }
    void Set(string state,string? detail=null) {State=state;Detail=detail;Changed?.Invoke();}
    async Task<string> StopReverse()
    {
        string result="graceful_stop";
        foreach(var component in components.Reverse())
        {
            if(component.Process is not null) await component.StopAsync();
            // Cleanup may already have released the process. Keep its terminal
            // outcome until an explicit successful start establishes a new run.
            if(component.State=="failed" || component.Detail=="stop_failed") result="stop_failed";
            else if(component.Detail=="forced_stop" && result!="stop_failed") result="forced_stop";
        }
        return result;
    }
    internal async Task StartAsync()
    {
        long requested;
        lock(intent) requested=stopEpoch;
        await operations.WaitAsync();
        try
        {
            lock(intent) {if(requested!=stopEpoch) return;}
            if(State=="ready") return;
            Set("starting");
            foreach(var component in components)
            {
                bool cancelled;
                lock(intent) {cancelled=requested!=stopEpoch;if(!cancelled)starting=component;}
                if(!cancelled) await component.StartAsync();
                lock(intent) {starting=null;cancelled=requested!=stopEpoch;}
                if(cancelled || component.State!="ready")
                {
                    string? failure=component.Detail;
                    await StopReverse();
                    Set(cancelled ? "stopped" : "failed",cancelled ? "start_cancelled" : failure);
                    return;
                }
            }
            Set("ready");
        }
        finally {lock(intent)starting=null;operations.Release();}
    }
    internal void CancelPendingStart()
    {
        lock(intent){stopEpoch++;starting?.CancelPendingStart();}
    }
    internal void RequestForceStop()
    {
        CancelPendingStart();
        foreach(var component in components) component.RequestForceStop();
    }
    internal async Task StopAsync()
    {
        CancelPendingStart();
        await operations.WaitAsync();
        try {Set("stopping");Set("stopped",await StopReverse());}
        finally{operations.Release();}
    }
    internal async Task CheckAsync()
    {
        if(!await operations.WaitAsync(0))return;
        try
        {
            if(State!="ready")return;
            foreach(var component in components)
            {
                await component.CheckAsync();
                if(component.State=="ready")continue;
                string? failure=component.Detail;
                await StopReverse();Set("failed",failure);return;
            }
        }
        finally{operations.Release();}
    }
    public void Dispose(){foreach(var component in components.Reverse())component.Dispose();operations.Dispose();}
}
