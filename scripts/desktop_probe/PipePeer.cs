// SPDX-License-Identifier: MIT
using System.IO.Pipes;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Qiban.DesktopProbe;

internal static class PipePeer
{
    internal static Task WriteLine(PipeStream pipe,string line,CancellationToken token=default)
        =>pipe.WriteAsync(System.Text.Encoding.UTF8.GetBytes(line+"\n").AsMemory(),token).AsTask();
    internal static void WriteLineSync(PipeStream pipe,string line)
        =>pipe.Write(System.Text.Encoding.UTF8.GetBytes(line+"\n"));

    internal static async Task<bool> Accept(NamedPipeServerStream pipe,Task processExit)
    {
        using var cancellation=new CancellationTokenSource();
        var connection=pipe.WaitForConnectionAsync(cancellation.Token);
        if(await Task.WhenAny(connection,processExit)==processExit)
        {
            cancellation.Cancel();
            try{await connection;}catch(OperationCanceledException){}
            return false;
        }
        await connection;
        return true;
    }
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe,out uint pid);
    [DllImport("kernel32.dll",SetLastError=true)]
    static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe,out uint pid);
    internal static int Client(PipeStream pipe)=>GetNamedPipeClientProcessId(pipe.SafePipeHandle,out var id)?checked((int)id):throw new IOException("management_peer:"+Marshal.GetLastWin32Error());
    internal static int Server(PipeStream pipe)=>GetNamedPipeServerProcessId(pipe.SafePipeHandle,out var id)?checked((int)id):throw new IOException("management_peer:"+Marshal.GetLastWin32Error());
    internal static async Task<string?> ReadLine(StreamReader reader,CancellationToken token,int maximum=65536)
    {
        var result=new System.Text.StringBuilder();var buffer=new char[1];
        while(await reader.ReadAsync(buffer.AsMemory(),token)>0)
        {
            if(buffer[0]=='\n')return result.ToString().TrimEnd('\r');
            if(result.Length>=maximum)throw new IOException("management_message_size");
            result.Append(buffer[0]);
        }
        return result.Length==0?null:result.ToString();
    }
}
