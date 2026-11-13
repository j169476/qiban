// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class RecoveryTests
{
    // Only orchestration is simulated. No Windows prompt or actual recovery is performed here.
    internal static async Task<string[]> Run()
    {
        var cases=new List<string>();
        foreach(string result in new[]{"Verified","Canceled","NotConfiguredForUser","DeviceBusy","DisabledByPolicy","RetriesExhausted","Unavailable","cancel-token"})
        {
            var operations=new List<string>();
            Task<JsonElement> Exchange(string operation,object arguments)
            {
                operations.Add(operation);
                return Task.FromResult(JsonSerializer.SerializeToElement(new{ok=true,result=new{challenge="test-challenge"}}));
            }
            var flow=new LockRecovery(Exchange,token=>result=="cancel-token"?Task.FromException<string>(new OperationCanceledException()):Task.FromResult(result));
            var response=await flow.Run("Synthetic-Recovery-Password",CancellationToken.None);
            var expected=result=="Verified"?new[]{"begin","complete"}:new[]{"begin","cancel"};
            if(!operations.SequenceEqual(expected)||response.GetProperty("ok").GetBoolean()!=(result=="Verified"))throw new IOException("recovery_gate_failed");
            cases.Add("native_verification_gate_"+result);
            operations.Clear();
            var restore=new BackupRestore(Exchange,token=>result=="cancel-token"?Task.FromException<string>(new OperationCanceledException()):Task.FromResult(result));
            var restored=await restore.Run("test-preview",CancellationToken.None);
            if(!operations.SequenceEqual(expected)||restored.GetProperty("ok").GetBoolean()!=(result=="Verified"))throw new IOException("restore_gate_failed");
            cases.Add("native_restore_verification_gate_"+result);
        }
        bool verified=false;
        var invalid=new LockRecovery((_,_)=>Task.FromResult(JsonSerializer.SerializeToElement(new{ok=false,error="invalid_password"})),
            _=>{verified=true;return Task.FromResult("Verified");});
        await invalid.Run("short",CancellationToken.None);
        if(verified)throw new IOException("invalid_password_started_windows_prompt");
        cases.Add("invalid_new_password_does_not_prompt");
        return cases.ToArray();
    }
}
