// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal sealed record StorageSpec(string WorkerExecutable,string ConfigurationPath,string LogDirectory);
internal sealed record StackSpec(string PostgresSpecPath,string StorageSpecPath);

internal static class StorageComponent
{
    // One fixed argument that turns the same worker into the uninstall preparation backend: the
    // store and local management open, but no model, platform connector, scheduler or automatic run.
    internal const string BackupOnlyArgument = "--backup-only";
    static bool BackupOperation(JsonElement request)=>request.TryGetProperty("action",out var action) &&
        (action.GetString()=="desktop-restore" || action.GetString()=="management" && request.TryGetProperty("operation",out var operation)
         && operation.ValueKind==JsonValueKind.String && operation.GetString()!.StartsWith("backups.",StringComparison.Ordinal));
    static bool WeChatSetup(JsonElement request)=>request.TryGetProperty("operation",out var operation)
        && operation.ValueKind==JsonValueKind.String && operation.GetString() is "wechat.directory" or "wechat.directory_page" or "wechat.select" or "wechat.targets.verify" or "wechat.targets.save" or "wechat.account_details";
    static bool PortableOperation(JsonElement request,out bool cancel)
    {
        cancel=false;
        if(!request.TryGetProperty("action",out var action)||action.ValueKind!=JsonValueKind.String
            ||action.GetString()!=DesktopPortable.Action)return false;
        cancel=request.TryGetProperty("operation",out var operation)&&operation.ValueKind==JsonValueKind.String
            &&operation.GetString()==DesktopPortable.CancelOperation;
        return true;
    }
    // A portable transfer answers on the backend's own budget (240s, 60s to cancel);
    // every other request keeps its previous deadline. The reply read here and the
    // Call deadline below must stay matched at worker+5s, or a slow switch would be
    // reported as a lost backend and the page would offer a retry it must not make.
    static int ReplySeconds(JsonElement request)=>PortableOperation(request,out bool cancel)
        ?(cancel?DesktopPortable.CancelSeconds:DesktopPortable.OperationSeconds)
        :BackupOperation(request)?120:WeChatSetup(request)?55:20;
    static int CallMilliseconds(JsonElement request)=>PortableOperation(request,out bool cancel)
        ?(cancel?DesktopPortable.CancelSeconds:DesktopPortable.OperationSeconds)*1000+5000
        :BackupOperation(request)?125000:WeChatSetup(request)?58000:10000;
    internal static int ReplySecondsForTest(JsonElement request)=>ReplySeconds(request);
    internal static int CallMillisecondsForTest(JsonElement request)=>CallMilliseconds(request);
    internal static StorageSpec Load(string path) => JsonSerializer.Deserialize<StorageSpec>(File.ReadAllText(path)) ?? throw new IOException("storage_spec_invalid");
    static string Evidence(StorageSpec spec,int pid) => Path.Combine(spec.LogDirectory,$"storage-{pid}.json");
    internal static ComponentDefinition Definition(string path,string pipe)
    {
        path=Path.GetFullPath(path);
        return ProbeService.Definition(pipe) with
        {
            Id="encrypted-storage",StartupTimeout=TimeSpan.FromSeconds(30),StopTimeout=TimeSpan.FromSeconds(15),
            Launch=()=>
            {
                var spec=Load(path);
                foreach(var file in new[]{spec.WorkerExecutable,spec.ConfigurationPath})
                    if(!Path.IsPathFullyQualified(file)||!File.Exists(file))throw new IOException("storage_runtime_missing");
                if(!Path.IsPathFullyQualified(spec.LogDirectory))throw new IOException("storage_spec_invalid");
                RuntimeBundle.Validate(spec.WorkerExecutable);
                return Program.BackupOnly
                    ? OwnedProcess.Start(Environment.ProcessPath!,"--storage-worker",path,pipe,Environment.ProcessId.ToString(),BackupOnlyArgument)
                    : OwnedProcess.Start(Environment.ProcessPath!,"--storage-worker",path,pipe,Environment.ProcessId.ToString());
            },
            ExitReason=process=>
            {
                string evidence=Evidence(Load(path),process.Id);
                if(!File.Exists(evidence))return "storage_process_exited";
                using var report=JsonDocument.Parse(File.ReadAllText(evidence));
                return report.RootElement.GetProperty("error").GetString() ?? "storage_process_exited";
            }
        };
    }

    internal static ComponentGroup CreateStack(string path,string pipe)
    {
        var spec=JsonSerializer.Deserialize<StackSpec>(File.ReadAllText(path)) ?? throw new IOException("stack_spec_invalid");
        return new ComponentGroup(PostgresComponent.Definition(spec.PostgresSpecPath,pipe+"-database"),
            Definition(spec.StorageSpecPath,pipe+"-storage"));
    }

    internal static async Task<int> Run(string path,string pipeName,int parentId,bool backupOnly=false)
    {
        using var parent=Process.GetProcessById(parentId);
        _ = parent.Handle;
        var spec=Load(path);
        Directory.CreateDirectory(spec.LogDirectory);
        var info=new ProcessStartInfo(spec.WorkerExecutable)
        {UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,
         RedirectStandardError=true,WorkingDirectory=Path.GetDirectoryName(spec.WorkerExecutable)!};
        info.ArgumentList.Add("--config");info.ArgumentList.Add(spec.ConfigurationPath);
        if(backupOnly)info.ArgumentList.Add("--backup-only");
#if QIBAN_SIWC_VALIDATION
        info.ArgumentList.Add("--authentication-route");info.ArgumentList.Add("siwc-validation");
#endif
        info.ArgumentList.Add("--application-session");
        info.ArgumentList.Add($"desktop-{parent.Id}-{parent.StartTime.ToUniversalTime().Ticks}");
        info.Environment.Remove("CODEX_THREAD_ID");
        // The bundled backend inherits this worker's Job. No credential enters arguments or this bridge.
        using var process=Process.Start(info)!;
        var processExit=process.WaitForExitAsync();
        var errors=process.StandardError.BaseStream.CopyToAsync(Stream.Null);
        async Task Expect(string field)
        {
            string? line=await process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(20));
            if(line is null)throw new IOException("storage_process_exited");
            using var response=JsonDocument.Parse(line);
            if(response.RootElement.TryGetProperty(field,out var ok)&&ok.ValueKind==JsonValueKind.True)return;
            throw new IOException(response.RootElement.TryGetProperty("error",out var code)?code.GetString():"storage_protocol");
        }
        try
        {
            await Expect("ready");
            File.WriteAllText(Path.Combine(spec.LogDirectory,"storage-process.json"),JsonSerializer.Serialize(new{worker=Environment.ProcessId,pid=process.Id}));
            while(!process.HasExited)
            {
                using var pipe=new NamedPipeServerStream(pipeName,PipeDirection.InOut,1,
                    PipeTransmissionMode.Byte,PipeOptions.CurrentUserOnly|PipeOptions.Asynchronous);
                if(!await PipePeer.Accept(pipe,processExit))break;
                if(parent.HasExited || PipePeer.Client(pipe)!=parent.Id)continue;
                using var reader=new StreamReader(pipe,leaveOpen:true);
                using var deadline=new CancellationTokenSource(2000);
                // Same 262144 bound as the response read, so a 24000-byte YAML record
                // survives JSON escaping of non-ASCII text (for example emoji).
                string? command=await PipePeer.ReadLine(reader,deadline.Token,262144);
                if(command is not null && command.StartsWith("{",StringComparison.Ordinal))
                {
                    using var document=JsonDocument.Parse(command);
                    // WebView JSON contains literal Unicode. Escape it before crossing
                    // Windows locale-dependent redirected stdin (notably emoji on CP936).
                    await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(document.RootElement));await process.StandardInput.FlushAsync();
                    string? reply=await process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(ReplySeconds(document.RootElement)));
                    if(reply is null)throw new IOException("storage_process_exited");
                    await PipePeer.WriteLine(pipe,reply);
                    continue;
                }
                switch(command)
                {
                    case "status":
                        await process.StandardInput.WriteLineAsync("{\"action\":\"status\"}");await process.StandardInput.FlushAsync();
                        await Expect("ready");
                        await PipePeer.WriteLine(pipe,$"{ProbeService.Version} {Environment.ProcessId} ready");break;
                    case "stop":
                        await PipePeer.WriteLine(pipe,"stopping");
                        await process.StandardInput.WriteLineAsync("{\"action\":\"stop\"}");await process.StandardInput.FlushAsync();
                        await Expect("stopped");await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
                        return process.ExitCode;
                }
            }
            throw new IOException("storage_process_exited");
        }
        catch(Exception error)
        {
            // Only our worker's fixed diagnostic codes, never Python stderr or credentials.
            string code=error is IOException && error.Message.StartsWith("storage_",StringComparison.Ordinal)?error.Message:"storage_runtime_failed";
            File.WriteAllText(Evidence(spec,Environment.ProcessId),JsonSerializer.Serialize(new{error=code}));
            return 1;
        }
        finally
        {
            if(!process.HasExited)process.Kill();
            await process.WaitForExitAsync();await errors;
        }
    }

    internal static async Task<JsonElement> Call(string pipeName,Process expectedServer,JsonElement request)
    {
        using var deadline=new CancellationTokenSource(CallMilliseconds(request));
        using var pipe=new NamedPipeClientStream(".",pipeName,PipeDirection.InOut,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
        await pipe.ConnectAsync(deadline.Token);
        if(expectedServer.HasExited || PipePeer.Server(pipe)!=expectedServer.Id)throw new IOException("management_peer");
        using var reader=new StreamReader(pipe,leaveOpen:true);
        await PipePeer.WriteLine(pipe,request.GetRawText(),deadline.Token);
        string? response=await PipePeer.ReadLine(reader,deadline.Token,262144);
        using var document=JsonDocument.Parse(response??throw new IOException("management_unavailable"));
        return document.RootElement.Clone();
    }

    internal static async Task<int> Host(string spec,string evidence)
    {
        string? configured=Environment.GetEnvironmentVariable("QIBAN_TEST_STACK_TIMEOUT_SECONDS");
        int deadlineSeconds=90;
        if(configured is not null && (!int.TryParse(configured,out deadlineSeconds) || deadlineSeconds<1 || deadlineSeconds>3600))
            throw new ArgumentException("Invalid stack test host deadline");
        Directory.CreateDirectory(evidence);
        using var group=CreateStack(spec,"qiban-stack-"+Guid.NewGuid().ToString("N"));
        void Save()
        {
            var value=new{state=group.State,detail=group.Detail,members=group.Snapshot,deadline_seconds=deadlineSeconds};
            File.WriteAllText(Path.Combine(evidence,"state.json"),JsonSerializer.Serialize(value));
            File.AppendAllText(Path.Combine(evidence,"events.jsonl"),JsonSerializer.Serialize(value)+"\n");
        }
        group.Changed+=Save;
        try
        {
            await group.StartAsync();Save();
            if(group.State!="ready")return 1;
            var clock=Stopwatch.StartNew();
            while(!File.Exists(Path.Combine(evidence,"stop")))
            {
                if(clock.Elapsed.TotalSeconds>deadlineSeconds)throw new TimeoutException("Stack test host deadline");
                await group.CheckAsync();if(group.State!="ready")return 1;
                await Task.Delay(200);
            }
            await group.StopAsync();Save();
            return group.Detail=="graceful_stop"?0:1;
        }
        finally{if(group.Process is not null)await group.StopAsync();}
    }
}
