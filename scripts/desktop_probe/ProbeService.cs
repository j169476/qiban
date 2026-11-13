// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.IO.Pipes;

namespace Qiban.DesktopProbe;

internal static class ProbeService
{
    internal const string Version = "1";
    internal static ComponentDefinition Definition(string name, string mode = "normal", int startupMs = 5000, int stopMs = 2000)
        => new("desktop-verification", TimeSpan.FromMilliseconds(startupMs), TimeSpan.FromMilliseconds(stopMs),
            () => OwnedProcess.Start(Environment.ProcessPath!, "--probe-service", name, mode),
            async (process, token) =>
            {
                string? response = await Exchange(name, process, "status", token);
                if (response is null) return false;
                var fields = response.Split(' ');
                if (fields.Length != 3 || fields[0] != Version) throw new IOException("protocol_version_mismatch");
                if (!int.TryParse(fields[1], out int pid) || pid != process.Id) throw new IOException("process_identity_mismatch");
                return fields[2] == "ready";
            },
            async (process, token) =>
            {
                if (await Exchange(name, process, "stop", token) != "stopping") throw new IOException("stop_not_acknowledged");
            });

    static async Task<string?> Exchange(string name, Process expectedServer, string request, CancellationToken token)
    {
        using var attempt = CancellationTokenSource.CreateLinkedTokenSource(token);
        attempt.CancelAfter(1000);
        using var pipe = new NamedPipeClientStream(".", name, PipeDirection.InOut,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        try
        {
            await pipe.ConnectAsync(attempt.Token);
            if(expectedServer.HasExited || PipePeer.Server(pipe)!=expectedServer.Id)throw new IOException("process_identity_mismatch");
            using var reader = new StreamReader(pipe, leaveOpen: true);
            await PipePeer.WriteLine(pipe,request,attempt.Token);
            return await reader.ReadLineAsync(attempt.Token);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { return null; }
    }

    internal static int Run(string name, string mode, Process? parent=null)
    {
        if (mode == "exit") return 7;
        while (true)
        {
            using var pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1,
                PipeTransmissionMode.Byte, PipeOptions.CurrentUserOnly);
            pipe.WaitForConnection();
            if(parent is not null && (parent.HasExited || PipePeer.Client(pipe)!=parent.Id))continue;
            using var reader = new StreamReader(pipe, leaveOpen: true);
            switch (reader.ReadLine())
            {
                case "status":
                    PipePeer.WriteLineSync(pipe,$"{(mode == "version" ? "2" : Version)} {(mode == "identity" ? 0 : Environment.ProcessId)} {(mode == "never-ready" ? "starting" : "ready")}");
                    break;
                case "stop":
                    PipePeer.WriteLineSync(pipe,"stopping");
                    if (mode == "ignore-stop") Thread.Sleep(Timeout.Infinite);
                    if (mode == "slow-stop") Thread.Sleep(700);
                    return mode == "fail-stop" ? 1 : 0;
            }
        }
    }
}
