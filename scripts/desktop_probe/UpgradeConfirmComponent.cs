// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Qiban.DesktopProbe;

// The member between the database and the backend. Before ordinary business may open this root,
// one program start has to settle an unfinished upgrade transaction: a staged one is confirmed by
// the program that is running, and any other unfinished one stops the start.
//
// The decision belongs to the backend verb (`--upgrade-maintenance startup-confirm`); this member
// only carries its one answer. It never supplies a program directory - the backend derives the
// directory of the program whose code is running from its own executable - and it never passes
// backend text on: an answer that is not a known verdict becomes one fixed code.
internal static class UpgradeConfirmComponent
{
    internal const string Failed = "upgrade_startup_failed";
    const string TimedOut = "upgrade_startup_timeout";
    static readonly Regex Code = new("^[a-z0-9_]{1,64}$", RegexOptions.CultureInvariant);

    static string SpecPath(string root)=>Path.Combine(root,"storage-spec.json");
    static string ResultPath(string root)=>Path.Combine(StorageComponent.Load(SpecPath(root)).LogDirectory,
        "upgrade-startup.json");

    internal static ComponentDefinition Definition(string root,string pipe)=>ProbeService.Definition(pipe) with
    {
        Id="upgrade-confirm",StartupTimeout=TimeSpan.FromSeconds(180),StopTimeout=TimeSpan.FromSeconds(5),
        Launch=()=>OwnedProcess.Start(Environment.ProcessPath!,"--upgrade-confirm-worker",root,pipe,Environment.ProcessId.ToString()),
        ExitReason=process=>Reason(root,process.Id)
    };

    // The verdict of one answer, or the fixed code that stops this start. A confirmed upgrade whose
    // cleanup left work behind is still a ready answer: its warning is carried, never deduced here.
    internal static string? Outcome(JsonElement value)
    {
        if(value.ValueKind!=JsonValueKind.Object)return Failed;
        if(value.TryGetProperty("ready",out var ready) && ready.ValueKind==JsonValueKind.True)return null;
        if(value.TryGetProperty("error",out var error) && error.ValueKind==JsonValueKind.String)
            return Fixed(error.GetString());
        return Failed;
    }

    internal static string? Outcome(string? answer)
    {
        if(answer is null)return Failed;
        try
        {
            using var value=JsonDocument.Parse(answer);
            return Outcome(value.RootElement);
        }
        catch(JsonException){return Failed;}
    }

    static string Fixed(string? code)=>code is not null && Code.IsMatch(code) ? code : Failed;

    // Only this run's own answer may explain this run's process, so a stale file is never read.
    internal static string Reason(string root,int pid)
    {
        try
        {
            using var value=JsonDocument.Parse(File.ReadAllText(ResultPath(root)));
            var result=value.RootElement;
            if(result.ValueKind!=JsonValueKind.Object
               || !result.TryGetProperty("pid",out var reported) || reported.GetInt32()!=pid)return Failed;
            if(result.TryGetProperty("code",out var code) && code.ValueKind==JsonValueKind.String)
                return Fixed(code.GetString());
            return Failed;
        }
        catch(Exception error) when(error is IOException or JsonException or InvalidOperationException
                                         or UnauthorizedAccessException or FormatException)
        {return Failed;}
    }

    static void Publish(string root,string? code,JsonElement? outcome)
    {
        try
        {
            string path=ResultPath(root);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path,JsonSerializer.Serialize(new{pid=Environment.ProcessId,code,outcome}));
        }
        catch(Exception error) when(error is IOException or JsonException or UnauthorizedAccessException
                                         or InvalidOperationException)
        {/* Diagnostics never decide a start. */}
    }

    internal static async Task<int> Run(string root,string pipe,int parentId)
    {
        using var parent=Process.GetProcessById(parentId);
        _ = parent.Handle;
        string worker;
        try{worker=StorageComponent.Load(SpecPath(root)).WorkerExecutable;}
        catch(Exception error) when(error is IOException or JsonException or UnauthorizedAccessException)
        {
            Publish(root,error is JsonException ? "storage_spec_invalid" : Fixed(error.Message),null);
            return 1;
        }
        var info=new ProcessStartInfo(worker)
        {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,
         RedirectStandardInput=true,WorkingDirectory=Path.GetDirectoryName(worker)!};
        foreach(var argument in new[]{"--upgrade-maintenance","startup-confirm","--data-dir",root})
            info.ArgumentList.Add(argument);
        info.Environment.Remove("CODEX_THREAD_ID");
        string? failure=Failed;
        JsonElement? outcome=null;
        try
        {
            using var process=Process.Start(info) ?? throw new IOException("worker_unavailable");
            process.StandardInput.Close();
            var output=process.StandardOutput.ReadToEndAsync();
            var errors=process.StandardError.BaseStream.CopyToAsync(Stream.Null);
            try
            {
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(170));
                string answer=await output;
                outcome=Document(answer);
                failure=outcome is JsonElement value ? Outcome(value) : Failed;
                // A ready answer from a process that still failed is not an answer to rely on.
                if(failure is null && process.ExitCode!=0)failure=Failed;
            }
            catch(TimeoutException){failure=TimedOut;}
            finally
            {
                if(!process.HasExited)process.Kill();
                await process.WaitForExitAsync();await errors;
            }
        }
        catch(Exception error) when(error is IOException or System.ComponentModel.Win32Exception)
        {
            // A worker that cannot even be started leaves nothing to rely on: a fixed code, no text.
            failure=Fixed(error.Message);
        }
        Publish(root,failure,outcome);
        if(failure is not null)return 1;
        // Keep the confirming member alive until reverse-order group stop, like the other workers.
        return ProbeService.Run(pipe,"normal",parent);
    }

    static JsonElement? Document(string answer)
    {
        try
        {
            using var value=JsonDocument.Parse(answer);
            return value.RootElement.Clone();
        }
        catch(JsonException){return null;}
    }
}
