// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class ComponentTests
{
    static void Require(bool value, string message) { if (!value) throw new Exception(message); }

    internal static async Task<int> Run(string directory)
    {
        Directory.CreateDirectory(directory);
        var results = new List<object>();
        try
        {
            foreach (var scenario in new[] {"normal", "version", "identity", "never-ready", "exit", "ignore-stop", "fail-stop", "cancel", "crash", "missing", "wait-natural", "wait-force", "wait-no-ack"})
                results.Add(await Check(scenario, directory));
            File.WriteAllText(Path.Combine(directory,"component-result.json"), JsonSerializer.Serialize(new {passed=true,results}));
            return 0;
        }
        catch (Exception error)
        {
            File.WriteAllText(Path.Combine(directory,"component-result.json"), JsonSerializer.Serialize(new {passed=false,results,error=error.ToString()}));
            return 1;
        }
    }

    static async Task<object> Check(string scenario, string directory)
    {
        var observations = new List<string>();
        var processes = new List<Process>();
        string mode = scenario is "cancel" ? "never-ready" : scenario is "crash" ? "normal" : scenario;
        var definition = ProbeService.Definition("qiban-component-"+Guid.NewGuid().ToString("N"),
            scenario=="wait-natural"?"slow-stop":scenario.StartsWith("wait-")?"ignore-stop":mode,
            startupMs: scenario == "never-ready" ? 800 : 5000, stopMs: scenario.StartsWith("wait-")?100:500);
        if(scenario.StartsWith("wait-")) definition=definition with {WaitForExplicitForce=true};
        if(scenario=="wait-no-ack") definition=definition with {RequestStop=(_,_)=>throw new IOException("synthetic_lost_ack")};
        int launches = 0;
        var originalLaunch = definition.Launch;
        definition = definition with { Launch = () =>
        {
            launches++;
            var owned = scenario == "missing" ? OwnedProcess.Start(Path.Combine(directory,"missing.exe")) : originalLaunch();
            var observed = Process.GetProcessById(owned.Process.Id);
            _ = observed.Handle;
            processes.Add(observed);
            return owned;
        }};
        using var component = new ManagedComponent(definition);
        component.Changed += () => observations.Add(component.State+":"+component.Detail);
        var clock = Stopwatch.StartNew();
        try
        {
            if(scenario.StartsWith("wait-"))
            {
                await component.StartAsync();Require(component.State=="ready","Wait case not ready");
                var stop=component.StopAsync();await Task.Delay(350);
                Require(!stop.IsCompleted && !processes[0].HasExited && component.State=="stopping","Database wait automatically killed process");
                if(scenario!="wait-natural")component.RequestForceStop();
                await stop.WaitAsync(TimeSpan.FromSeconds(5));
                Require(component.Detail==(scenario=="wait-natural"?"graceful_stop":"forced_stop"),"Wait outcome incorrect");
                await component.StopAsync();
                Require(component.Detail==(scenario=="wait-natural"?"graceful_stop":"forced_stop"),"Repeated stop changed wait outcome");
            }
            else if (scenario == "cancel")
            {
                var first = component.StartAsync();
                while (component.Process is null) await Task.Delay(10);
                var queued = component.StartAsync();
                var stop = component.StopAsync();
                await Task.WhenAll(first, queued, stop);
                Require(component.State == "stopped" && launches == 1, "Stop must cancel both active and queued starts");
                await Task.Delay(300);
                Require(component.Process is null && launches == 1, "Stopped component restarted itself");
            }
            else if (scenario == "normal")
            {
                await Task.WhenAll(component.StartAsync(), component.StartAsync(), component.StartAsync());
                Require(component.State == "ready" && launches == 1, "Concurrent starts must share one ready instance");
                await component.StopAsync();
                Require(component.Detail == "graceful_stop" && processes[0].ExitCode == 0, "Normal stop did not exit gracefully");
                await component.StartAsync();
                Require(component.State == "ready" && launches == 2, "Explicit restart did not work");
                await component.StopAsync();
                Require(component.State == "stopped" && processes[1].ExitCode == 0, "Restart stop failed");
            }
            else if (scenario == "crash")
            {
                await component.StartAsync();
                Require(component.State == "ready", "Crash case not ready");
                processes[0].Kill(); await processes[0].WaitForExitAsync();
                await component.CheckAsync();
                Require(component.State == "failed" && component.Process is null && launches == 1, "Crash not reported");
                await component.CheckAsync();
                Require(launches == 1, "Health check restarted failed component");
                await component.StopAsync();
                Require(component.State=="failed" && component.Detail=="health_check_failed", "Stop erased crash diagnostic");
                await component.StartAsync();
                Require(component.State == "ready" && launches == 2, "Manual crash recovery failed");
                await component.StopAsync();
            }
            else if (scenario == "fail-stop")
            {
                await component.StartAsync(); Require(component.State == "ready", "Failed stop case not ready");
                await component.StopAsync();
                Require(component.State == "stopped" && component.Detail == "stop_failed", "Nonzero stop was misreported as graceful");
                await component.StopAsync();
                Require(component.Detail=="stop_failed", "Repeated stop erased failed termination");
            }
            else if (scenario == "ignore-stop")
            {
                await component.StartAsync(); Require(component.State == "ready", "Forced stop case not ready");
                await component.StopAsync();
                Require(component.State == "stopped" && component.Detail == "forced_stop", "Forced cleanup not distinguished");
                await component.StopAsync();
                Require(component.Detail=="forced_stop", "Repeated stop erased forced termination");
            }
            else
            {
                await component.StartAsync();
                Require(component.State == "failed" && component.Process is null && launches == 1, "Startup failure not terminal");
                string? expected = scenario switch { "version"=>"protocol_version_mismatch", "identity"=>"process_identity_mismatch",
                    "never-ready"=>"startup_timeout", "exit"=>"process_exited_before_ready", _=>null };
                if (expected is not null) Require(component.Detail == expected, "Wrong startup failure: "+component.Detail);
            }
            Require(processes.All(p=>p.HasExited), "Owned process survived completed operation");
            Require(clock.ElapsedMilliseconds < 10000, "Operation exceeded test deadline");
            return new {scenario,passed=true,launches,elapsed_ms=clock.ElapsedMilliseconds,observations,all_processes_exited=true};
        }
        finally
        {
            await component.StopAsync();
            foreach (var process in processes) process.Dispose();
        }
    }
}
