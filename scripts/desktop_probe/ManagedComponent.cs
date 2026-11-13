// SPDX-License-Identifier: MIT
using System.Diagnostics;

namespace Qiban.DesktopProbe;

internal sealed record ComponentDefinition(string Id, TimeSpan StartupTimeout, TimeSpan StopTimeout,
    Func<OwnedProcess> Launch, Func<Process, CancellationToken, Task<bool>> IsReady,
    Func<Process, CancellationToken, Task> RequestStop, Func<Process,string>? ExitReason = null,
    bool WaitForExplicitForce = false);

// Serializes lifecycle operations. Readiness retries concern the same owned process;
// a failed component is never restarted without a new explicit Start request.
internal sealed class ManagedComponent(ComponentDefinition definition) : IDisposable
{
    readonly SemaphoreSlim operations = new(1, 1);
    readonly object intent = new();
    CancellationTokenSource? startup;
    CancellationTokenSource forceStop = new();
    OwnedProcess? owner;
    long stopEpoch;
    bool disposed;
    internal string Id => definition.Id;
    internal string State { get; private set; } = "stopped";
    internal string? Detail { get; private set; }
    internal string? HealthDiagnostic { get; private set; }
    internal Process? Process => owner?.Process;
    internal event Action? Changed;

    void Set(string state, string? detail = null)
    {
        State = state; Detail = detail; Changed?.Invoke();
    }
    void Release()
    {
        owner?.Dispose(); owner = null;
    }

    internal async Task StartAsync()
    {
        long requested;
        lock (intent) { if (disposed) return; requested = stopEpoch; }
        await operations.WaitAsync();
        try
        {
            lock (intent)
            {
                if (disposed || requested != stopEpoch) return;
                if (owner?.Process is { HasExited: false } && State == "ready") return;
                startup = new CancellationTokenSource(definition.StartupTimeout);
            }
            try
            {
                Release(); forceStop.Dispose(); forceStop=new(); HealthDiagnostic=null; Set("starting");
                owner = definition.Launch();
                while (true)
                {
                    startup.Token.ThrowIfCancellationRequested();
                    if (owner.Process.HasExited) throw new IOException(definition.ExitReason?.Invoke(owner.Process) ?? "process_exited_before_ready");
                    if (await definition.IsReady(owner.Process, startup.Token)) break;
                    await Task.Delay(100, startup.Token);
                }
                startup.Token.ThrowIfCancellationRequested();
                Set("ready");
            }
            catch (OperationCanceledException)
            {
                Release();
                lock (intent) Set(requested != stopEpoch || disposed ? "stopped" : "failed",
                    requested != stopEpoch || disposed ? "start_cancelled" : "startup_timeout");
            }
            catch (Exception error) when (error is IOException or System.ComponentModel.Win32Exception)
            {
                Release(); Set("failed", error.Message);
            }
            finally
            {
                lock (intent) { startup?.Dispose(); startup = null; }
            }
        }
        finally { operations.Release(); }
    }

    internal void CancelPendingStart()
    {
        lock (intent) { stopEpoch++; startup?.Cancel(); }
    }

    internal void RequestForceStop() { CancelPendingStart(); forceStop.Cancel(); owner?.Terminate(); }

    internal async Task StopAsync()
    {
        CancelPendingStart();
        await operations.WaitAsync();
        try
        {
            if (owner is null) { if(State!="failed") Set("stopped",Detail); return; }
            Set("stopping");
            bool forced = forceStop.IsCancellationRequested;
            if (!owner.Process.HasExited)
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(forceStop.Token);
                if(!definition.WaitForExplicitForce) timeout.CancelAfter(definition.StopTimeout);
                try
                {
                    timeout.Token.ThrowIfCancellationRequested();
                    try { await definition.RequestStop(owner.Process, timeout.Token); }
                    catch(IOException) when(definition.WaitForExplicitForce)
                    {
                        // A failed acknowledgement does not authorize killing a database.
                        Set("stopping","stop_request_failed");
                    }
                    await owner.Process.WaitForExitAsync(timeout.Token);
                }
                catch (Exception error) when (error is IOException or OperationCanceledException)
                { forced = true; }
            }
            bool unsuccessful = owner.Process.HasExited && owner.Process.ExitCode != 0;
            Release();
            Set("stopped", forced ? "forced_stop" : unsuccessful ? "stop_failed" : "graceful_stop");
        }
        finally { operations.Release(); }
    }

    internal async Task CheckAsync()
    {
        if (!await operations.WaitAsync(0)) return;
        try
        {
            if (State != "ready" || owner is null) return;
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(1));
            try
            {
                if (!owner.Process.HasExited && await definition.IsReady(owner.Process, timeout.Token)) return;
                HealthDiagnostic=owner.Process.HasExited?"process_exited:"+owner.Process.ExitCode:"readiness_false";
                Release(); Set("failed", "health_check_failed");
            }
            catch (Exception error) when (error is IOException or OperationCanceledException)
            { HealthDiagnostic=error.GetType().Name+":"+error.HResult+":"+error.Message; Release(); Set("failed", "health_check_failed"); }
        }
        finally { operations.Release(); }
    }

    // Call only after lifecycle tasks have completed; host death is covered by Job ownership.
    public void Dispose()
    {
        lock (intent) { disposed = true; stopEpoch++; }
        Release(); forceStop.Dispose(); operations.Dispose();
    }
}
