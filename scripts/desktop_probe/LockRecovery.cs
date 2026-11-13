// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// Verification is supplied by the native host, never by page arguments or model output.
internal sealed class LockRecovery(Func<string,object,Task<JsonElement>> exchange,
    Func<CancellationToken,Task<string>> verify)
{
    internal async Task<JsonElement> Run(string password,CancellationToken token)
    {
        var start=await exchange("begin",new{password});
        if(!start.GetProperty("ok").GetBoolean())return start;
        string challenge=start.GetProperty("result").GetProperty("challenge").GetString()!;
        string verified;
        try{verified=await verify(token);}
        catch(OperationCanceledException){verified="Canceled";}
        if(verified!="Verified")
        {
            await exchange("cancel",new{});
            return JsonSerializer.SerializeToElement(new{ok=false,error="windows_verification",message=WindowsVerification.Message(verified)});
        }
        token.ThrowIfCancellationRequested();
        return await exchange("complete",new{challenge});
    }
}
