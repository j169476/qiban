// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.IO.Pipes;
using System.Net.NetworkInformation;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal sealed record PostgresSpec(string BinDirectory, string DataDirectory, string LogDirectory,
    int Port, string MajorVersion, string OwnerToken);

internal static class PostgresComponent
{
    internal static PostgresSpec Load(string path)
    {
        try { return JsonSerializer.Deserialize<PostgresSpec>(File.ReadAllText(path)) ?? throw new IOException("database_spec_invalid"); }
        catch (JsonException error) { throw new IOException("database_spec_invalid",error); }
    }

    static void Validate(PostgresSpec spec)
    {
        if (string.IsNullOrWhiteSpace(spec.BinDirectory) || string.IsNullOrWhiteSpace(spec.DataDirectory)
            || string.IsNullOrWhiteSpace(spec.LogDirectory) || string.IsNullOrWhiteSpace(spec.MajorVersion)
            || spec.Port < 1024 || spec.Port > 65535 || !Path.IsPathFullyQualified(spec.BinDirectory)
            || !Path.IsPathFullyQualified(spec.DataDirectory) || !Path.IsPathFullyQualified(spec.LogDirectory))
            throw new IOException("database_spec_invalid");
        foreach (var name in new[] {"pg_ctl.exe", "postgres.exe", "pg_isready.exe"})
            if (!File.Exists(Path.Combine(spec.BinDirectory, name))) throw new IOException("database_runtime_missing");
        if (spec.OwnerToken is not {Length: >=32} || File.ReadAllText(Path.Combine(spec.DataDirectory,"qiban-probe-owner")).Trim() != spec.OwnerToken)
            throw new IOException("database_owner_mismatch");
        if (File.ReadAllText(Path.Combine(spec.DataDirectory,"PG_VERSION")).Trim() != spec.MajorVersion)
            throw new IOException("database_version_mismatch");
    }

    internal static ComponentDefinition Definition(string specPath, string pipe)
    {
        specPath = Path.GetFullPath(specPath);
        return ProbeService.Definition(pipe) with
        {
            Id = "postgresql", StartupTimeout = TimeSpan.FromSeconds(30), StopTimeout = TimeSpan.FromSeconds(15),
            WaitForExplicitForce = true,
            Launch = () =>
            {
                Validate(Load(specPath));
                return OwnedProcess.Start(Environment.ProcessPath!, "--postgres-worker", specPath, pipe, Environment.ProcessId.ToString());
            }
        };
    }

    static async Task<(int Code, string Output)> Control(PostgresSpec spec, string binary, params string[] arguments)
    {
        // This helper inherits the worker's Job; pg_ctl start is separately owned below.
        var info = new ProcessStartInfo(Path.Combine(spec.BinDirectory,binary))
        { UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true,
          RedirectStandardOutput=true, RedirectStandardError=true, WorkingDirectory=spec.BinDirectory };
        foreach (string argument in arguments) info.ArgumentList.Add(argument);
        using var process = Process.Start(info)!;
        process.StandardInput.Close();
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(12));
        try { await process.WaitForExitAsync(deadline.Token); }
        catch (OperationCanceledException) { process.Kill(); await process.WaitForExitAsync(); throw new IOException("database_control_timeout"); }
        return (process.ExitCode, (await output)+(await error));
    }

    static async Task<Process> WaitForOwnedServer(PostgresSpec spec, OwnedProcess launcher)
    {
        // pg_ctl may observe a previous ready PID file during rapid crash recovery.
        // Wait for a live member of this launch's Job; never delete the stale file.
        var deadline = Stopwatch.StartNew();
        while (deadline.Elapsed < TimeSpan.FromSeconds(20))
        {
            string? first = null;
            try { first = File.ReadLines(Path.Combine(spec.DataDirectory,"postmaster.pid")).FirstOrDefault(); }
            catch (IOException) { /* The server is still replacing its PID file. */ }
            if (int.TryParse(first, out int pid))
            {
                Process? candidate = null;
                try { candidate = Process.GetProcessById(pid); _ = candidate.Handle; }
                catch (ArgumentException) { candidate?.Dispose(); candidate = null; }
                if (candidate is not null)
                {
                    if (candidate.HasExited) candidate.Dispose();
                    else if (launcher.Contains(candidate)) return candidate;
                    else { candidate.Dispose(); throw new IOException("database_process_not_owned"); }
                }
            }
            await Task.Delay(50);
        }
        throw new IOException("database_owned_process_timeout");
    }

    internal static async Task<int> Run(string specPath, string pipeName, int parentId)
    {
        using var parent=Process.GetProcessById(parentId);
        _ = parent.Handle;
        var spec = Load(specPath);
        Directory.CreateDirectory(spec.LogDirectory);
        void Record(string stage, object? detail=null) => File.AppendAllText(Path.Combine(spec.LogDirectory,"database-events.jsonl"),
            JsonSerializer.Serialize(new {stage,worker=Environment.ProcessId,detail})+"\n");
        try
        {
            Validate(spec);
            using var installLock = new FileStream(spec.DataDirectory+".qiban-lock",
                FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            var version = await Control(spec,"pg_ctl.exe","--version");
            if (version.Code != 0 || !version.Output.StartsWith("pg_ctl (PostgreSQL) "+spec.MajorVersion+".", StringComparison.Ordinal))
                throw new IOException("database_binary_version_mismatch");
            var status = await Control(spec,"pg_ctl.exe","-D",spec.DataDirectory,"status");
            if (status.Code == 0) throw new IOException("database_already_running");
            if (status.Code != 3) throw new IOException("database_status_failed");
            if (IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners().Any(endpoint=>endpoint.Port==spec.Port))
                throw new IOException("database_port_in_use");
            Record("starting",new {version=version.Output.Trim(),port=spec.Port});
            using var launcher = OwnedProcess.Start(Path.Combine(spec.BinDirectory,"pg_ctl.exe"),
                "-D",spec.DataDirectory,"-o","-p "+spec.Port.ToString(),"-l",Path.Combine(spec.LogDirectory,"postgres.log"),"-w","-t","15","start");
            await launcher.Process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(20));
            if (launcher.Process.ExitCode != 0) throw new IOException("database_start_failed");
            using var server = await WaitForOwnedServer(spec,launcher);
            int pid = server.Id;
            var readiness = Stopwatch.StartNew();
            while (true)
            {
                if(server.HasExited) throw new IOException("database_process_exited");
                var ready = await Control(spec,"pg_isready.exe","-h","127.0.0.1","-p",spec.Port.ToString(),
                    "-U","qiban_probe","-d","postgres","-t","2","-q");
                if(ready.Code==0) break;
                if(ready.Code is not (1 or 2) || readiness.Elapsed.TotalSeconds>20)
                    throw new IOException("database_not_ready");
                await Task.Delay(100);
            }
            Record("ready",new {pid,owned=true});
            var serverExit=server.WaitForExitAsync();
            File.WriteAllText(Path.Combine(spec.LogDirectory,"database-process.json"),
                JsonSerializer.Serialize(new {worker=Environment.ProcessId,pid,owned=true}));
            while (!server.HasExited)
            {
                using var pipe = new NamedPipeServerStream(pipeName,PipeDirection.InOut,1,
                    PipeTransmissionMode.Byte,PipeOptions.CurrentUserOnly|PipeOptions.Asynchronous);
                if(!await PipePeer.Accept(pipe,serverExit))break;
                if(parent.HasExited || PipePeer.Client(pipe)!=parent.Id)continue;
                using var reader = new StreamReader(pipe,leaveOpen:true);
                using var requestLimit = new CancellationTokenSource(2000);
                switch (await reader.ReadLineAsync(requestLimit.Token))
                {
                    case "status":
                        await PipePeer.WriteLine(pipe,$"{ProbeService.Version} {Environment.ProcessId} {(server.HasExited ? "failed" : "ready")}");
                        break;
                    case "stop":
                        await PipePeer.WriteLine(pipe,"stopping");
                        // Never let a changed PID file direct pg_ctl at an unrelated instance.
                        if (int.Parse(File.ReadLines(Path.Combine(spec.DataDirectory,"postmaster.pid")).First()) != server.Id
                            || !launcher.Contains(server)) throw new IOException("database_stop_identity_mismatch");
                        Record("stopping");
                        // Send once, then wait for the owned server itself. No timeout may
                        // dispose its Job while PostgreSQL is still flushing its checkpoint.
                        try
                        {
                            var stopped = await Control(spec,"pg_ctl.exe","-D",spec.DataDirectory,"-m","fast","-W","stop");
                            if(stopped.Code!=0) Record("stop_request_failed",new{exit_code=stopped.Code});
                        }
                        catch(IOException) { Record("stop_request_failed",new{reason="control_unavailable"}); }
                        await server.WaitForExitAsync();
                        Record("stopped",new {exit_code=server.ExitCode});
                        return server.ExitCode == 0 ? 0 : 1;
                }
            }
            throw new IOException("database_process_exited");
        }
        catch (Exception error)
        {
            Record("failed",new {type=error.GetType().Name,error.Message});
            return 1;
        }
    }

    // Controlled integration-test host; no credentials or production configuration are loaded.
    internal static async Task<int> Host(string specPath, string evidence, int deadlineSeconds=60)
    {
        if(deadlineSeconds<1 || deadlineSeconds>3600)return 2;
        Directory.CreateDirectory(evidence);
        using var component = new ManagedComponent(Definition(specPath,"qiban-postgres-"+Guid.NewGuid().ToString("N")));
        void Save() => File.WriteAllText(Path.Combine(evidence,"state.json"),
            JsonSerializer.Serialize(new {state=component.State,detail=component.Detail,worker=component.Process?.Id}));
        component.Changed += Save;
        try
        {
            await component.StartAsync(); Save();
            if(component.State!="ready") return 1;
            var clock=Stopwatch.StartNew();
            while(!File.Exists(Path.Combine(evidence,"stop")))
            {
                if(clock.Elapsed.TotalSeconds>deadlineSeconds) throw new TimeoutException("Database test host deadline");
                await component.CheckAsync();
                if(component.State!="ready") return 1;
                await Task.Delay(100);
            }
            await component.StopAsync(); Save();
            return component.Detail=="graceful_stop" ? 0 : 1;
        }
        catch(Exception error)
        {
            string code=error is TimeoutException?"database_test_host_timeout":"database_test_host_failed";
            File.WriteAllText(Path.Combine(evidence,"host-error.json"),JsonSerializer.Serialize(new{error=code}));
            Console.Error.WriteLine(code);
            return 1;
        }
        finally { if(component.Process is not null) await component.StopAsync(); }
    }
}
