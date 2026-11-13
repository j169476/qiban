// SPDX-License-Identifier: MIT
using System.Security.Cryptography;
using System.Text.Json;

namespace Qiban.DesktopProbe;

// Detect incomplete/mixed application resources. Not a publisher signature.
internal static class RuntimeBundle
{
    internal static void Validate(string executable)
        => Validate(Path.GetDirectoryName(executable)!,Path.GetFileName(executable),"storage_bundle_invalid");

    internal static void Validate(string root,string entrypoint,string errorCode)
    {
        try
        {
            string executable=Path.Combine(root,entrypoint.Replace('/',Path.DirectorySeparatorChar));
            using var manifest=JsonDocument.Parse(File.ReadAllText(Path.Combine(root,"runtime-manifest.json")));
            var value=manifest.RootElement;
            if(value.GetProperty("format").GetInt32()!=1 ||
               value.GetProperty("entrypoint").GetString()!=entrypoint)throw new InvalidDataException();
            var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(var entry in value.GetProperty("files").EnumerateArray())
            {
                string relative=entry.GetProperty("path").GetString() ?? "";
                string path=Path.GetFullPath(Path.Combine(root,relative));
                if(string.IsNullOrEmpty(relative)||Path.IsPathFullyQualified(relative)||
                   !path.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)||!names.Add(path))
                    throw new InvalidDataException();
                using var stream=File.OpenRead(path);
                string actual=Convert.ToHexString(SHA256.HashData(stream));
                if(!actual.Equals(entry.GetProperty("sha256").GetString(),StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException();
            }
            if(!names.Contains(executable)||names.Count<2)throw new InvalidDataException();
        }
        catch(Exception error) when(error is IOException or InvalidDataException or UnauthorizedAccessException or JsonException or
                                    InvalidOperationException or KeyNotFoundException or ArgumentException)
        {throw new IOException(errorCode);}
    }
}
