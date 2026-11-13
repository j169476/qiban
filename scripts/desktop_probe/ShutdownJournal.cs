// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// No account, message, credential or database exception text belongs in this journal.
internal sealed class ShutdownJournal(string directory)
{
    internal sealed record Entry(int Format,string Run,string Stage,DateTimeOffset At);
    readonly string path=Path.Combine(directory,"application-session.json");
    readonly string history=Path.Combine(directory,"application-lifecycle.jsonl");
    readonly string run=Guid.NewGuid().ToString("N");
    internal bool NeedsRecovery {get;private set;}
    bool verified,abnormal;

    internal void Begin()
    {
        Directory.CreateDirectory(directory);
        if(File.Exists(path))
        {
            Entry previous;
            try { previous=JsonSerializer.Deserialize<Entry>(File.ReadAllText(path)) ?? throw new JsonException(); }
            catch(JsonException error) { throw new IOException("shutdown_journal_invalid",error); }
            if(previous.Format!=1 || previous.Stage is not ("running" or "verified" or "closing" or "clean" or "forced" or "failed"))
                throw new IOException("shutdown_journal_invalid");
            NeedsRecovery=previous.Stage!="clean";
            Append(previous); // Preserve the prior run even if power died before its append.
        }
        Write("running");
    }
    internal void Verified() { verified=true; Write("verified"); }
    internal void Closing() => Write("closing");
    internal void Abnormal(bool forced=false) { abnormal=true; Write(forced?"forced":"failed"); }
    internal void Complete(bool graceful)
    {
        if(graceful && !abnormal && (!NeedsRecovery || verified)) Write("clean");
        else if(!abnormal) Abnormal();
    }
    void Append(Entry entry)
    {
        using var stream=new FileStream(history,FileMode.Append,FileAccess.Write,FileShare.Read);
        var bytes=System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(entry)+"\n");
        stream.Write(bytes);stream.Flush(true);
    }
    void Write(string stage)
    {
        var entry=new Entry(1,run,stage,DateTimeOffset.UtcNow);
        using(var stream=new FileStream(path+".tmp",FileMode.Create,FileAccess.Write,FileShare.None))
        { JsonSerializer.Serialize(stream,entry);stream.Flush(true); }
        File.Move(path+".tmp",path,true);
        Append(entry);
    }
}
