// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Globalization;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Qiban.DesktopProbe;

// One answer of the maintenance backend, reduced to the fixed fields this window may show.
internal sealed record UpgradeHostReply(bool Ok,string? Error,string? Phase,string? Preservation,
    string? Sealed,bool Retained=false)
{
    internal static UpgradeHostReply Failed(string error)=>new(false,error,null,null,null);
}

// The bridge between this window and one maintenance backend, in the three steps this application
// already uses for its workers:
//
//   window    owns a Job and starts `--upgrade-host-bridge` through OwnedProcess, so the bridge and
//             everything the bridge starts are killed with this window - on cancel, on close and on
//             a forced kill alike. It then connects to the bridge's current-user-only pipe and hands
//             over the Windows-verification receipt the host just produced.
//   bridge    accepts only its own parent on that pipe, and starts the redirected backend with
//             `--upgrade-host`, feeding it that receipt on its stdin. The receipt therefore only
//             travels from this window through a subprocess this window owns onto a backend's
//             stdin - never a file, a command-line flag or the environment.
//   backend   installs the receipt as its upgrade.verification channel and runs exactly one verb.
//
// Only fixed answer fields are read back, and only fixed text is ever shown.
internal static class UpgradeRecoveryHost
{
    internal const string BridgeMode="--upgrade-host-bridge";
    internal const string BackendVerb="--upgrade-host";
    const int Limit=16384;
    const string FailedText="{\"ok\":false,\"error\":\"upgrade_host_failed\"}";
    static readonly Regex Code=new("\\A[a-z0-9_]{1,64}\\z",RegexOptions.CultureInvariant);
    static readonly Regex Display=new("\\A[^\\x00-\\x1f/\\\\:]{1,200}\\z",RegexOptions.CultureInvariant);
    static readonly TimeSpan Deadline=TimeSpan.FromMinutes(30);
    static readonly TimeSpan Handover=TimeSpan.FromSeconds(60);

    // The receipt is one ISO-8601 UTC instant and one opaque session id: no identity, no password
    // and nothing a page could have supplied. Only this window knows it, and it lives for one verb.
    internal static string Receipt(string session)=>JsonSerializer.Serialize(new{session,
        at=DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'+00:00'",CultureInfo.InvariantCulture)});

    // -- the window's side ---------------------------------------------------

    internal static async Task<UpgradeHostReply> Execute(string worker,string root,string installed,
        string verb,CancellationToken token)
    {
        string pipeName="qiban-upgrade-host-"+Guid.NewGuid().ToString("N");
        string receipt=Receipt(Guid.NewGuid().ToString("N"));
        // A blocked backend must never outlive this window: the Job handle is the owner's, so the
        // bridge, the backend and any PostgreSQL server the backend owns die with it.
        OwnedProcess? bridge=null;
        // Before the receipt is handed over nothing has run at all; afterwards the backend may be
        // working, so any failure is an unknown result and never "nothing happened".
        bool handed=false;
        try
        {
            bridge=OwnedProcess.Start(Environment.ProcessPath!,BridgeMode,root,installed,worker,verb,
                pipeName,Environment.ProcessId.ToString());
            using var deadline=CancellationTokenSource.CreateLinkedTokenSource(token);
            deadline.CancelAfter(Deadline);
            using var client=new NamedPipeClientStream(".",pipeName,PipeDirection.InOut,
                PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
            await client.ConnectAsync(deadline.Token);
            if(bridge.Process.HasExited || PipePeer.Server(client)!=bridge.Process.Id)
                return UpgradeHostReply.Failed("upgrade_host_start");
            using var reader=new StreamReader(client,leaveOpen:true);
            handed=true;
            await PipePeer.WriteLine(client,receipt,deadline.Token);
            return Read(await PipePeer.ReadLine(reader,deadline.Token,Limit));
        }
        catch(OperationCanceledException)
        {
            // Cancelled by this window or by the deadline; the Job kill below ends the backend.
            return UpgradeHostReply.Failed(handed?"upgrade_host_timeout":"upgrade_host_start");
        }
        catch(Exception error) when(error is IOException or System.ComponentModel.Win32Exception
            or InvalidOperationException)
        {
            return UpgradeHostReply.Failed(handed?"upgrade_host_failed":"upgrade_host_start");
        }
        finally
        {
            if(bridge is not null)
            {
                // Disposing kills the whole child tree and closes the Job; a cleanup failure here
                // cannot change the answer that was already read.
                try{bridge.Dispose();}catch(Exception){}
            }
        }
    }

    // -- the bridge's own mode ----------------------------------------------

    internal static async Task<int> Bridge(string root,string installed,string worker,string verb,
        string pipeName,int parentId)
    {
        try
        {
            using var parent=Process.GetProcessById(parentId);
            _ = parent.Handle;
            using var pipe=new NamedPipeServerStream(pipeName,PipeDirection.InOut,1,
                PipeTransmissionMode.Byte,PipeOptions.CurrentUserOnly|PipeOptions.Asynchronous);
            using var handover=new CancellationTokenSource(Handover);
            await pipe.WaitForConnectionAsync(handover.Token);
            // The receipt may only come from the window that started this process, never from a peer.
            if(parent.HasExited || PipePeer.Client(pipe)!=parent.Id)return 1;
            using var reader=new StreamReader(pipe,leaveOpen:true);
            string? receipt=await PipePeer.ReadLine(reader,handover.Token,4096);
            if(receipt is null || receipt.Trim().Length==0)return 1;
            using var deadline=new CancellationTokenSource(Deadline);
            await PipePeer.WriteLine(pipe,await Run(worker,root,installed,verb,receipt,deadline.Token));
            return 0;
        }
        catch(Exception)
        {
            // A refused or broken handover starts no verb and leaves no console dialog behind.
            return 1;
        }
    }

    // The redirected backend: one verb, this receipt on its stdin, one bounded answer line.
    static async Task<string> Run(string worker,string root,string installed,string verb,string receipt,
        CancellationToken token)
    {
        if(!File.Exists(worker))return "{\"ok\":false,\"error\":\"upgrade_host_missing\"}";
        var info=new ProcessStartInfo(worker)
        {
            UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=Path.GetDirectoryName(worker)!,
            RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,
            StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=new UTF8Encoding(false)
        };
        foreach(var argument in new[]{BackendVerb,root,installed,verb})info.ArgumentList.Add(argument);
        info.Environment.Remove("CODEX_THREAD_ID");
        // The backend inherits this bridge's Job, which is the window's Job: it cannot be left behind.
        using var process=Process.Start(info) ?? throw new IOException("upgrade_host_start");
        var errors=process.StandardError.BaseStream.CopyToAsync(Stream.Null);
        try
        {
            await process.StandardInput.WriteLineAsync(receipt);
            await process.StandardInput.FlushAsync();
            process.StandardInput.Close();
            string? answer=await process.StandardOutput.ReadLineAsync(token);
            await process.WaitForExitAsync(token);
            return Forward(answer,process.ExitCode);
        }
        finally
        {
            if(!process.HasExited)process.Kill(true);
            await process.WaitForExitAsync();
            await errors;
        }
    }

    // An answer that says success from a process that still failed is not an answer to rely on, and
    // an answer that is not one JSON object is discarded instead of being passed on.
    static string Forward(string? answer,int exitCode)
    {
        if(answer is null || answer.Length>Limit)return FailedText;
        try
        {
            using var document=JsonDocument.Parse(answer);
            if(document.RootElement.ValueKind!=JsonValueKind.Object)return FailedText;
            bool ok=document.RootElement.TryGetProperty("ok",out var flag)&&flag.ValueKind==JsonValueKind.True;
            return ok && exitCode!=0 ? FailedText : answer;
        }
        catch(JsonException){return FailedText;}
    }

    // -- reading one answer -------------------------------------------------

    // Only fixed fields are read back: a verdict, a stable code, a phase, one display name and the
    // one flag that says the previous build came back over files nobody published. Backend text
    // never reaches this window.
    static UpgradeHostReply Read(string? answer)
    {
        if(answer is null)return UpgradeHostReply.Failed("upgrade_host_failed");
        try
        {
            using var document=JsonDocument.Parse(answer);
            var value=document.RootElement;
            if(value.ValueKind!=JsonValueKind.Object)return UpgradeHostReply.Failed("upgrade_host_failed");
            bool ok=value.TryGetProperty("ok",out var flag)&&flag.ValueKind==JsonValueKind.True;
            bool retained=value.TryGetProperty("retained",out var kept)&&kept.ValueKind==JsonValueKind.True;
            return new UpgradeHostReply(ok,ok?null:Fixed(Text(value,"error")),Text(value,"phase"),
                Displayed(value,"preservation"),Displayed(value,"sealed"),retained);
        }
        catch(JsonException){return UpgradeHostReply.Failed("upgrade_host_failed");}
    }

    static string? Text(JsonElement value,string name)=>value.TryGetProperty(name,out var item)
        && item.ValueKind==JsonValueKind.String ? item.GetString() : null;

    static string Fixed(string? code)=>code is not null && Code.IsMatch(code) ? code : "upgrade_host_failed";

    static string? Displayed(JsonElement value,string name)
    {
        string? text=Text(value,name);
        return text is not null && Display.IsMatch(text) ? text : null;
    }
}
