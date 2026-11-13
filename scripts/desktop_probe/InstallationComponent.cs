// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class InstallationComponent
{
    static string Backend(string application)=>Path.Combine(application,"resources","storage","Qiban.Storage.exe");
    static string Database(string application)=>Path.Combine(application,"resources","postgres");
    static string ErrorPath(string root,int pid)=>Path.Combine(root,$"installation-result-{pid}.json");

    internal static ComponentDefinition Definition(string root,string application,string pipe,string pause="")
        => ProbeService.Definition(pipe) with
        {
            Id="installation",StartupTimeout=TimeSpan.FromSeconds(180),StopTimeout=TimeSpan.FromSeconds(5),
            Launch=()=>
            {
                RuntimeBundle.Validate(Backend(application));
                RuntimeBundle.Validate(Database(application),"bin/postgres.exe","database_bundle_invalid");
                return OwnedProcess.Start(Environment.ProcessPath!,"--installation-worker",root,application,pipe,pause,Environment.ProcessId.ToString());
            },
            ExitReason=process=>
            {
                var path=ErrorPath(root,process.Id);
                if(!File.Exists(path))return "installation_failed";
                using var result=JsonDocument.Parse(File.ReadAllText(path));
                return result.RootElement.GetProperty("error").GetString() ?? "installation_failed";
            }
        };

    // Dependency order: preparation, database, the startup decision on an unfinished upgrade,
    // then ordinary business. The confirming member sits after the database because confirming
    // reads the clone, and before the backend because the backend refuses to open a root that is
    // still staged - so a refused confirmation stops the database and never starts the backend.
    internal static ComponentGroup Group(string root,string application,string pipe)=>new(
        Definition(root,application,pipe+"-install"),
        PostgresComponent.Definition(Path.Combine(root,"postgres-spec.json"),pipe+"-database"),
        UpgradeConfirmComponent.Definition(root,pipe+"-upgrade"),
        StorageComponent.Definition(Path.Combine(root,"storage-spec.json"),pipe+"-storage"));

    internal static async Task<int> Run(string root,string application,string pipe,string pause,int parentId)
    {
        using var parent=Process.GetProcessById(parentId);
        _ = parent.Handle;
        var info=new ProcessStartInfo(Backend(application))
        {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,
         RedirectStandardInput=true,WorkingDirectory=application};
        foreach(var argument in new[]{"--initialize",root,"--postgres",Path.Combine(Database(application),"bin")})info.ArgumentList.Add(argument);
        if(pause!=""){info.ArgumentList.Add("--test-pause");info.ArgumentList.Add(pause);}
        info.Environment.Remove("CODEX_THREAD_ID");
        using var process=Process.Start(info)!;
        process.StandardInput.Close();
        var output=process.StandardOutput.ReadToEndAsync();
        var errors=process.StandardError.BaseStream.CopyToAsync(Stream.Null);
        try
        {
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(170));
            using var value=JsonDocument.Parse(await output);
            if(process.ExitCode!=0 || !value.RootElement.TryGetProperty("ready",out var ready)||ready.ValueKind!=JsonValueKind.True)
            {
                string code=value.RootElement.TryGetProperty("error",out var error)?error.GetString()??"installation_failed":"installation_failed";
                throw new IOException(code);
            }
            // Keep the preparation component alive until reverse-order group stop.
            return ProbeService.Run(pipe,"normal",parent);
        }
        catch(Exception error)
        {
            Directory.CreateDirectory(root);
            string code=error is IOException ? error.Message : "installation_failed";
            File.WriteAllText(ErrorPath(root,Environment.ProcessId),JsonSerializer.Serialize(new{error=code}));
            return 1;
        }
        finally
        {
            if(!process.HasExited)process.Kill();
            await process.WaitForExitAsync();await errors;
        }
    }

    internal static async Task<int> Host(string root,string application,string evidence,string pause)
    {
        Directory.CreateDirectory(evidence);
        using var component=new ManagedComponent(Definition(root,application,"qiban-install-"+Guid.NewGuid().ToString("N"),pause));
        void Save()=>File.WriteAllText(Path.Combine(evidence,"state.json"),JsonSerializer.Serialize(new{state=component.State,detail=component.Detail,worker=component.Process?.Id}));
        component.Changed+=Save;
        await component.StartAsync();Save();
        if(component.State!="ready")return 1;
        await component.StopAsync();Save();
        return component.Detail=="graceful_stop"?0:1;
    }
}
