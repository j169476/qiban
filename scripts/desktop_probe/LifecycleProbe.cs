// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class LifecycleProbe
{
    internal static int Run(string[] args)
    {
        if (args[0] == "--lifecycle-leaf") { Thread.Sleep(Timeout.Infinite); return 0; }
        string directory = Path.GetFullPath(args[1]);
        Directory.CreateDirectory(directory);
        if (args[0] == "--lifecycle-worker")
        {
            var info = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute=false, CreateNoWindow=true };
            info.ArgumentList.Add("--lifecycle-leaf");
            using var leaf = Process.Start(info)!;
            File.WriteAllText(Path.Combine(directory,"worker.json"), JsonSerializer.Serialize(new {pid=Environment.ProcessId,child=leaf.Id,received=args.Skip(2).ToArray()}));
            Thread.Sleep(Timeout.Infinite);
            return 0;
        }
        if (args[0] == "--lifecycle-owner")
        {
            using var owned = OwnedProcess.Start(Environment.ProcessPath!, "--lifecycle-worker", directory, "", "a b", "quote\"value", "end\\");
            File.WriteAllText(Path.Combine(directory,"owner.json"), JsonSerializer.Serialize(new {pid=Environment.ProcessId,child=owned.Process.Id}));
            var wait = Stopwatch.StartNew();
            while (!File.Exists(Path.Combine(directory,"stop")))
            {
                if (wait.ElapsedMilliseconds > 30000) return 2;
                Thread.Sleep(20);
            }
            return 0;
        }
        if (args[0] == "--lifecycle-invalid")
        {
            try { using var owned = OwnedProcess.Start(Path.Combine(directory,"missing.exe")); return 3; }
            catch (System.ComponentModel.Win32Exception) { return 0; }
        }
        return 4;
    }
}
