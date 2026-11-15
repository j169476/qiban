// SPDX-License-Identifier: MIT
using System.Security.Cryptography;
using System.Text;

namespace Qiban.DesktopProbe;

internal static class ProfileCleanup
{
    internal const string Switch = "--clear-profile-for-uninstall";
    internal static string DefaultRoot => new AppPaths(null).Root;
    internal static bool IsDefault(string path) => string.Equals(
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(path)), DefaultRoot, StringComparison.OrdinalIgnoreCase);

    // The command accepts no path: only the current Windows user's default Qiban profile.
    internal static int Run()
    {
        try
        {
            if(Mutex.TryOpenExisting(Program.RunningMarker,out var running)) {running.Dispose();return 2;}
            string root=DefaultRoot;
            string instance="Local\\QibanDesktopProbe-"+Convert.ToHexString(SHA256.HashData(
                Encoding.UTF8.GetBytes(Environment.UserName+"|"+root.ToUpperInvariant())))[..24];
            using var mutex=new Mutex(true,instance,out bool first);
            if(!first)return 2;
            try {DeleteTree(root,Path.GetDirectoryName(root)!);return 0;}
            finally {mutex.ReleaseMutex();}
        }
        catch(Exception error) when(error is IOException or UnauthorizedAccessException or ArgumentException)
        {return 1;}
    }

    // Testable with synthetic directories. Inspect the entire tree before any deletion,
    // reject junctions/symlinks, and recheck every entry as it is removed.
    internal static void DeleteTree(string root,string parent)
    {
        root=Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        parent=Path.TrimEndingDirectorySeparator(Path.GetFullPath(parent));
        if(!string.Equals(Path.GetDirectoryName(root),parent,StringComparison.OrdinalIgnoreCase)
            || string.Equals(root,Path.GetPathRoot(root),StringComparison.OrdinalIgnoreCase))
            throw new IOException("profile_path_invalid");
        for(var ancestor=new DirectoryInfo(root);ancestor is not null;ancestor=ancestor.Parent)
            if(ancestor.Exists)Check(ancestor.FullName,root,false);
        if(!Directory.Exists(root))return;
        Inspect(root,root);
        Remove(root,root);
    }
    static void Check(string path,string root,bool inside=true)
    {
        var full=Path.GetFullPath(path);
        if(inside && !string.Equals(full,root,StringComparison.OrdinalIgnoreCase) && !UninstallRule.IsInside(root,full))
            throw new IOException("profile_path_invalid");
        if((File.GetAttributes(full)&FileAttributes.ReparsePoint)!=0)throw new IOException("profile_link_refused");
    }
    static void Inspect(string directory,string root)
    {
        Check(directory,root);
        foreach(var path in Directory.EnumerateFileSystemEntries(directory))
        {
            Check(path,root);
            if(Directory.Exists(path))Inspect(path,root);
        }
    }
    static void Remove(string directory,string root)
    {
        Check(directory,root);
        foreach(var path in Directory.EnumerateFileSystemEntries(directory))
        {
            Check(path,root);
            if(Directory.Exists(path))Remove(path,root);else File.Delete(path);
        }
        Directory.Delete(directory,false);
    }
}
