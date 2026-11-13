// SPDX-License-Identifier: MIT
using System.IO.Pipes;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class PipeTests
{
    internal static async Task<int> Run(string path)
    {
        int rounds=0;bool passed=false;
        try
        {
            string name="qiban-pipe-boundary-"+Guid.NewGuid().ToString("N");
            var exit=new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            async Task Server()
            {
                for(int i=0;i<300;i++)
                {
                    using var pipe=new NamedPipeServerStream(name,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
                    if(!await PipePeer.Accept(pipe,exit.Task))throw new IOException("unexpected_pipe_cancel");
                    using var reader=new StreamReader(pipe,leaveOpen:true);
                    if(await reader.ReadLineAsync()!="status")throw new IOException("wrong_request");
                    await PipePeer.WriteLine(pipe,"ready");
                }
            }
            var server=Server();
            for(int i=0;i<300;i++)
            {
                if(i<12)await Task.Delay(i%2==0?499:501);
                using var pipe=new NamedPipeClientStream(".",name,PipeDirection.InOut,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
                using var deadline=new CancellationTokenSource(5000);
                await pipe.ConnectAsync(deadline.Token);
                using var reader=new StreamReader(pipe,leaveOpen:true);
                await PipePeer.WriteLine(pipe,"status",deadline.Token);
                if(await reader.ReadLineAsync(deadline.Token)!="ready")throw new IOException("reply_missing");
                rounds++;
            }
            await server;
            using(var pipe=new NamedPipeServerStream(name,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly))
            {
                var pending=PipePeer.Accept(pipe,exit.Task);exit.SetResult();
                if(await pending.WaitAsync(TimeSpan.FromSeconds(1)))throw new IOException("exit_not_observed");
            }
            passed=true;
        }
        finally{File.WriteAllText(path,JsonSerializer.Serialize(new{passed,rounds}));}
        return passed?0:1;
    }
}
