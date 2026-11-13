// SPDX-License-Identifier: MIT
namespace Qiban.DesktopProbe;

// The profile belongs to the user, independently of the installed executable.
// INSTALL-174: the default root is Qiban\Profile, a sibling of the installer's default program
// directory (Programs\Qiban), so uninstalling the program never reaches personal data. An explicit
// --data-dir still wins and no existing directory is moved: an older Qiban\DesktopProbe root is
// left exactly where it was.
internal sealed class AppPaths
{
    internal const string Folder = "Profile";

    internal string ApplicationDirectory { get; } = Path.GetDirectoryName(Environment.ProcessPath!)!;
    internal string Root { get; }
    internal string Data => Path.Combine(Root, "data");
    internal string Logs => Path.Combine(Root, "logs");
    internal string Cache => Path.Combine(Root, "cache");
    internal string Temporary => Path.Combine(Root, "temp");
    internal string Resources => Path.Combine(Root, "resources");
    internal string WebView => Path.Combine(Cache, "webview");

    internal AppPaths(string? profile)
    {
        Root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(profile ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Qiban", Folder)));
    }

    internal void Prepare()
    {
        foreach (var directory in new[] { Data, Logs, Cache, Temporary, Resources })
            Directory.CreateDirectory(directory);
        // Process-local settings are inherited by owned children, never saved globally.
        // The single-file .NET host may extract native libraries before Main executes.
        Environment.SetEnvironmentVariable("TEMP", Temporary);
        Environment.SetEnvironmentVariable("TMP", Temporary);
    }

    internal object Describe() => new
    {
        application = ApplicationDirectory, root = Root, data = Data, logs = Logs,
        cache = Cache, temporary = Temporary, resources = Resources, webview = WebView,
        working_directory = Environment.CurrentDirectory, runtime_temp = Path.GetTempPath()
    };
}
