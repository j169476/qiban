// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal sealed class BackupRestore(Func<string,object,Task<JsonElement>> exchange,
    Func<CancellationToken,Task<string>> verify)
{
    internal async Task<JsonElement> Run(string preview,CancellationToken token)
    {
        var start=await exchange("begin",new{token=preview});
        if(!start.GetProperty("ok").GetBoolean())return start;
        string challenge=start.GetProperty("result").GetProperty("challenge").GetString()!;
        string verified;
        try{verified=await verify(token);}
        catch(OperationCanceledException){verified="Canceled";}
        if(verified!="Verified")
        {
            await exchange("cancel",new{});
            return JsonSerializer.SerializeToElement(new{ok=false,error="windows_verification",message=WindowsVerification.RestoreMessage(verified)});
        }
        token.ThrowIfCancellationRequested();
        return await exchange("complete",new{challenge});
    }
}
