// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Web.WebView2.Core;

namespace Qiban.DesktopProbe;

// R1.5／R7.5: WebView2 is the window's only hard dependency. A missing runtime is the
// one startup condition the native application can repair, so it is prepared before the
// main window exists: no journal entry, event subscription or backend start can then be
// repeated by a second initialization. This is not a general downloader: one fixed
// Microsoft address, one trusted-publisher check and the official silent installer.
internal static class WebViewRuntime
{
    // The address the Microsoft distribution guide and the official WiX sample use for
    // the Evergreen bootstrapper. It is deliberately not configurable.
    internal const string InstallerAddress = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
    internal const string InstallerFileName = "MicrosoftEdgeWebview2Setup.exe";
    const long MaximumInstallerBytes = 64L * 1024 * 1024;
    const long BufferBytes = 81920;
    static readonly TimeSpan DownloadBudget = TimeSpan.FromMinutes(5);
    static readonly TimeSpan InstallBudget = TimeSpan.FromMinutes(10);
    static readonly object installerGate = new();
    static Process? installer;
    static string? installerSource;

    // Called from Main after the WinForms pipeline is configured and before the main
    // window exists. True means startup continues exactly as before; false means the
    // person chose to leave, so this process ends without starting a window.
    internal static bool EnsureReady(string temporaryDirectory)
    {
        var detection = Detect();
        if (detection.State == WebViewRuntimeState.Present) return true;
        // Only the SDK's own missing-runtime signal opens the preparation window. A
        // broken, blocked or unrecognized installation keeps the original startup text
        // and is never repaired automatically.
        if (detection.State != WebViewRuntimeState.Missing) return true;
        try
        {
            return WebViewRuntimeWindow.Show(temporaryDirectory, ProductionDelegates()) is { Prepared: true };
        }
        catch (Exception)
        {
            // A native window that cannot be shown must not leave an unprepared process
            // pretending to start; the original startup failure text reports it instead.
            return true;
        }
    }

    internal static PreparationDelegates ProductionDelegates() => new(Detect, DownloadAsync, VerifySignature, InstallAsync);

    // The SDK documents WebView2RuntimeNotFoundException for exactly this condition; the
    // call is local (registry/runtime files) and sends no network request.
    internal static WebViewRuntimeDetection Detect()
    {
        try
        {
            string? version = CoreWebView2Environment.GetAvailableBrowserVersionString(browserExecutableFolder: null);
            return string.IsNullOrEmpty(version)
                ? new(WebViewRuntimeState.Missing, null, "runtime_not_reported")
                : new(WebViewRuntimeState.Present, version, "runtime_reported");
        }
        catch (WebView2RuntimeNotFoundException)
        {
            return new(WebViewRuntimeState.Missing, null, "runtime_not_found");
        }
        catch (Exception error)
        {
            return new(WebViewRuntimeState.Unknown, null, error.GetType().Name);
        }
    }

    static bool StillRunning(Process? process)
    {
        if (process is null) return false;
        try { return !process.HasExited; }
        catch (InvalidOperationException) { return false; }
    }

    // A previous attempt may still be installing from the copy that was already verified
    // (the bounded wait ended first). That file is reused instead of downloading a second
    // installer or starting a second Microsoft installation.
    internal static async Task<string> DownloadAsync(string directory, IProgress<DownloadProgress> progress, CancellationToken token)
    {
        string? pending;
        lock (installerGate)
            pending = StillRunning(installer) && installerSource is { } source && File.Exists(source) ? source : null;
        if (pending is not null) return pending;
        Directory.CreateDirectory(directory);
        string target = Path.Combine(directory, InstallerFileName);
        string staging = target + ".partial";
        try
        {
            // HttpClient.Timeout and HttpCompletionOption.ResponseHeadersRead only bound the
            // headers, so the whole request including the body gets its own deadline. The
            // linked token covers every read/write/flush; an outside cancellation still
            // reports as cancelled, and only the deadline reports as a timeout.
            using var deadline = new CancellationTokenSource(DownloadBudget);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, deadline.Token);
            var linkedToken = linked.Token;
            using var client = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
            using var response = await client.GetAsync(InstallerAddress, HttpCompletionOption.ResponseHeadersRead, linkedToken).ConfigureAwait(false);
            if (response.RequestMessage?.RequestUri is not { Scheme: "https" })
                throw new RuntimeDownloadException("下载地址不是安全的 HTTPS 连接，已停止，请重试。");
            if (!response.IsSuccessStatusCode)
                throw new RuntimeDownloadException($"微软下载站点返回 {(int)response.StatusCode}，请稍后重试。");
            long? total = response.Content.Headers.ContentLength;
            if (total is long declared && (declared <= 0 || declared > MaximumInstallerBytes))
                throw new RuntimeDownloadException("下载内容大小异常，已停止，请重试。");
            long received = 0, reportedPercent = -1, reportedBytes = -1;
            var buffer = new byte[BufferBytes];
            await using var source = await response.Content.ReadAsStreamAsync(linkedToken).ConfigureAwait(false);
            await using (var file = new FileStream(staging, FileMode.Create, FileAccess.Write, FileShare.None, (int)BufferBytes, useAsync: true))
            {
                while (true)
                {
                    int count = await source.ReadAsync(buffer.AsMemory(), linkedToken).ConfigureAwait(false);
                    if (count <= 0) break;
                    received += count;
                    if (received > MaximumInstallerBytes)
                        throw new RuntimeDownloadException("下载内容超出预期大小，已停止，请重试。");
                    await file.WriteAsync(buffer.AsMemory(0, count), linkedToken).ConfigureAwait(false);
                    if (total is long known && known > 0)
                    {
                        long percent = received * 100 / known;
                        if (percent == reportedPercent) continue;
                        reportedPercent = percent;
                    }
                    else if (received == reportedBytes) continue;
                    // Without a declared length the received byte count is the progress.
                    reportedBytes = received;
                    progress.Report(new DownloadProgress(received, total));
                }
                await file.FlushAsync(linkedToken).ConfigureAwait(false);
            }
            if (received == 0) throw new RuntimeDownloadException("下载内容为空，请重试。");
            File.Move(staging, target, true);
            lock (installerGate) installerSource = target;
            return target;
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            throw;
        }
        catch (HttpRequestException error)
        {
            throw new RuntimeDownloadException("无法连接微软下载站点，请检查网络后重试。", error);
        }
        catch (OperationCanceledException)
        {
            throw new RuntimeDownloadException("下载超时，请重试。");
        }
        finally
        {
            if (File.Exists(staging))
            {
                try { File.Delete(staging); }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
            }
        }
    }

    // Trusted Windows signature plus the Microsoft publisher. Revocation covers the whole
    // chain except the root, exactly as WinTrust defines it: an offline or unavailable
    // revocation service fails verification and the file is rejected, never bypassed. The
    // official bootstrapper is replaced over time, so its hash is not a basis for trust;
    // the fixed URL and TLS are not either. Any failure discards the file.
    internal static SignatureVerdict VerifySignature(string installerPath)
    {
        if (!OperatingSystem.IsWindows()) return SignatureVerdict.Rejection("当前系统不支持 Windows 数字签名验证，已停止。");
        if (!File.Exists(installerPath)) return SignatureVerdict.Rejection("下载的安装程序不存在，已停止。");
        IntPtr pathPointer = IntPtr.Zero, fileInfoBlock = IntPtr.Zero;
        var action = ActionGenericVerifyV2;
        var data = new WinTrustData
        {
            Size = (uint)Marshal.SizeOf<WinTrustData>(),
            UiChoice = UiNone,
            RevocationChecks = RevokeWholeChain,
            UnionChoice = ChoiceFile,
            StateAction = StateActionVerify,
            ProviderFlags = RevocationCheckChainExcludeRoot
        };
        try
        {
            pathPointer = Marshal.StringToCoTaskMemUni(Path.GetFullPath(installerPath));
            fileInfoBlock = Marshal.AllocHGlobal(Marshal.SizeOf<WinTrustFileInfo>());
            Marshal.StructureToPtr(new WinTrustFileInfo
            {
                Size = (uint)Marshal.SizeOf<WinTrustFileInfo>(),
                Path = pathPointer
            }, fileInfoBlock, false);
            data.File = fileInfoBlock;
            if (WinVerifyTrust(IntPtr.Zero, ref action, ref data) != 0)
                return SignatureVerdict.Rejection("下载的文件未通过 Windows 数字签名验证，已丢弃且不会运行。");
            string? organization = SignerOrganization(data.StateData);
            if (organization is null)
                return SignatureVerdict.Rejection("无法读取下载文件的签名证书，已丢弃且不会运行。");
            if (!string.Equals(organization, "Microsoft Corporation", StringComparison.OrdinalIgnoreCase))
                return SignatureVerdict.Rejection("下载文件的签名发布者不是 Microsoft Corporation，已丢弃且不会运行。");
            return SignatureVerdict.MicrosoftPublisher;
        }
        catch (Exception error) when (error is ExternalException or ArgumentException or CryptographicException or IOException or InvalidOperationException)
        {
            return SignatureVerdict.Rejection("无法完成下载文件的签名验证，已丢弃且不会运行。");
        }
        finally
        {
            if (data.StateData != IntPtr.Zero)
            {
                data.StateAction = StateActionClose;
                _ = WinVerifyTrust(IntPtr.Zero, ref action, ref data);
            }
            if (fileInfoBlock != IntPtr.Zero) Marshal.FreeHGlobal(fileInfoBlock);
            if (pathPointer != IntPtr.Zero) Marshal.FreeCoTaskMem(pathPointer);
        }
    }

    static string? SignerOrganization(IntPtr stateData)
    {
        if (stateData == IntPtr.Zero) return null;
        IntPtr providerData = WTHelperProvDataFromStateData(stateData);
        if (providerData == IntPtr.Zero) return null;
        IntPtr signer = WTHelperGetProvSignerFromChain(providerData, 0, false, 0);
        if (signer == IntPtr.Zero) return null;
        IntPtr providerCertificate = WTHelperGetProvCertFromChain(signer, 0);
        if (providerCertificate == IntPtr.Zero) return null;
        IntPtr certificate = Marshal.PtrToStructure<CryptProviderCert>(providerCertificate).Certificate;
        if (certificate == IntPtr.Zero) return null;
        var context = Marshal.PtrToStructure<CertContext>(certificate);
        if (context.Encoded == IntPtr.Zero || context.EncodedSize == 0) return null;
        var encoded = new byte[context.EncodedSize];
        Marshal.Copy(context.Encoded, encoded, 0, encoded.Length);
        using var x509 = X509CertificateLoader.LoadCertificate(encoded);
        string decoded;
        try { decoded = new X500DistinguishedName(x509.Subject).Decode(X500DistinguishedNameFlags.None); }
        catch (CryptographicException) { return null; }
        foreach (string part in decoded.Split(','))
        {
            string field = part.Trim();
            if (field.Length < 3 || !field.StartsWith("O=", StringComparison.OrdinalIgnoreCase)) continue;
            return field[2..].Trim().Trim('"').Trim();
        }
        return null;
    }

    // The official installer runs hidden and without elevation (/silent /install). The
    // wait is bounded, but the Microsoft process is never terminated from here: after the
    // bound it stays owned so a retry re-checks and cannot start a duplicate installation.
    internal static async Task<int> InstallAsync(string installerPath)
    {
        Process process;
        lock (installerGate)
        {
            if (!StillRunning(installer))
            {
                installer?.Dispose();
                installer = null;
                string executable = Path.GetFullPath(installerPath);
                var start = new ProcessStartInfo(executable)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    WorkingDirectory = Path.GetDirectoryName(executable)!,
                    ErrorDialog = false
                };
                start.ArgumentList.Add("/silent");
                start.ArgumentList.Add("/install");
                Process started;
                try { started = Process.Start(start) ?? throw new RuntimeInstallException("官方安装程序未能启动，请重试。"); }
                catch (System.ComponentModel.Win32Exception error) { throw new RuntimeInstallException("官方安装程序未能启动，请重试。", error); }
                installer = started;
                installerSource = executable;
                process = started;
            }
            else process = installer ?? throw new RuntimeInstallException("官方安装程序状态不可用，请稍后重试检测。");
        }
        using var bounded = new CancellationTokenSource(InstallBudget);
        try
        {
            await process.WaitForExitAsync(bounded.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw new RuntimeInstallException("官方安装程序在等待上限内仍未结束，可能仍在后台安装；栖伴没有强制结束它，请稍后重试检测。");
        }
        int exitCode;
        try { exitCode = process.ExitCode; }
        catch (InvalidOperationException) { throw new RuntimeInstallException("无法读取官方安装程序的结果，请稍后重试检测。"); }
        lock (installerGate)
        {
            installer = null;
            installerSource = null;
            process.Dispose();
        }
        return exitCode;
    }

    // The downloaded copy lives in this application's own temp directory and is removed
    // once it is no longer needed. A file still used by a running installer stays.
    internal static void RemoveInstaller(string? installerPath)
    {
        if (string.IsNullOrEmpty(installerPath) || !File.Exists(installerPath)) return;
        lock (installerGate)
        {
            if (StillRunning(installer)) return;
            if (string.Equals(installerSource, installerPath, StringComparison.OrdinalIgnoreCase)) installerSource = null;
        }
        try { File.Delete(installerPath); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }

    [StructLayout(LayoutKind.Sequential)]
    struct WinTrustFileInfo
    {
        internal uint Size;
        internal IntPtr Path;
        internal IntPtr FileHandle;
        internal IntPtr KnownSubject;
    }

    // WINTRUST_DATA as exported by current Windows: the trailing signature settings are
    // always passed as null, and Size describes this layout.
    [StructLayout(LayoutKind.Sequential)]
    struct WinTrustData
    {
        internal uint Size;
        internal IntPtr PolicyCallbackData;
        internal IntPtr SipClientData;
        internal uint UiChoice;
        internal uint RevocationChecks;
        internal uint UnionChoice;
        internal IntPtr File;
        internal uint StateAction;
        internal IntPtr StateData;
        internal IntPtr UrlReference;
        internal uint ProviderFlags;
        internal uint UiContext;
        internal IntPtr SignatureSettings;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct CryptProviderCert
    {
        internal uint Size;
        internal IntPtr Certificate;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct CertContext
    {
        internal uint EncodingType;
        internal IntPtr Encoded;
        internal uint EncodedSize;
        internal IntPtr Info;
        internal IntPtr Store;
    }

    static readonly Guid ActionGenericVerifyV2 = new("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");
    const uint UiNone = 2;
    const uint RevokeWholeChain = 1;
    const uint ChoiceFile = 1;
    const uint StateActionVerify = 1;
    const uint StateActionClose = 2;
    const uint RevocationCheckChainExcludeRoot = 0x80;

    [DllImport("wintrust.dll", ExactSpelling = true, PreserveSig = true)]
    static extern int WinVerifyTrust(IntPtr window, ref Guid action, ref WinTrustData data);
    [DllImport("wintrust.dll", ExactSpelling = true)]
    static extern IntPtr WTHelperProvDataFromStateData(IntPtr stateData);
    [DllImport("wintrust.dll", ExactSpelling = true)]
    static extern IntPtr WTHelperGetProvSignerFromChain(IntPtr providerData, uint signerIndex,
        [MarshalAs(UnmanagedType.Bool)] bool counterSigner, uint counterSignerIndex);
    [DllImport("wintrust.dll", ExactSpelling = true)]
    static extern IntPtr WTHelperGetProvCertFromChain(IntPtr signer, uint certificateIndex);
}

internal enum WebViewRuntimeState { Present, Missing, Unknown }

internal readonly record struct WebViewRuntimeDetection(WebViewRuntimeState State, string? Version, string Detail);

internal readonly record struct DownloadProgress(long Received, long? Total);

internal readonly record struct SignatureVerdict(bool Trusted, string Message)
{
    internal static SignatureVerdict MicrosoftPublisher => new(true, "下载文件由 Microsoft Corporation 签发且通过 Windows 签名验证。");
    internal static SignatureVerdict Rejection(string message) => new(false, message);
}

internal sealed record PreparationDelegates(
    Func<WebViewRuntimeDetection> Detect,
    Func<string, IProgress<DownloadProgress>, CancellationToken, Task<string>> Download,
    Func<string, SignatureVerdict> Verify,
    Func<string, Task<int>> Install);

internal enum PreparationStage { Detecting, Downloading, Verifying, Installing, Rechecking }

internal readonly record struct PreparationStatus(PreparationStage Stage, string Text, int Percent);

// Progress callbacks run inline instead of being posted to a context: the caller can then
// rely on ordering, so a late download report cannot overwrite a later stage or the final
// result. The window's own sink marshals to its thread synchronously.
internal sealed class DirectProgress<T>(Action<T> report) : IProgress<T>
{
    public void Report(T value) => report(value);
}

internal enum PreparationOutcome
{
    Prepared, Cancelled, DownloadFailed, SignatureRejected, InstallFailed, StillMissing, Unknown, Failed
}

internal readonly record struct PreparationResult(PreparationOutcome Outcome, string? Version, string Message)
{
    internal bool Prepared => Outcome == PreparationOutcome.Prepared;
}

internal sealed class RuntimeDownloadException(string message, Exception? inner = null) : Exception(message, inner);

internal sealed class RuntimeInstallException(string message, Exception? inner = null) : Exception(message, inner);

// One preparation attempt, independent of any window so it can be exercised with
// injected delegates. Detection runs first, and detection runs again after the installer
// ends: an exit code of zero alone never lets startup continue.
internal static class WebViewRuntimePreparation
{
    internal static async Task<PreparationResult> RunAsync(PreparationDelegates delegates, string temporaryDirectory,
        IProgress<PreparationStatus> progress, CancellationToken token)
    {
        progress.Report(new(PreparationStage.Detecting, "正在检测本机的 WebView2 运行组件……", -1));
        var before = delegates.Detect();
        if (before.State == WebViewRuntimeState.Present)
            return new(PreparationOutcome.Prepared, before.Version, "本机已有 WebView2 运行组件，无需准备。");
        if (before.State != WebViewRuntimeState.Missing)
            return new(PreparationOutcome.Unknown, null, "无法确认本机是否缺少 WebView2 运行组件，未自动安装。");
        string? installer = null;
        try
        {
            progress.Report(new(PreparationStage.Downloading, "正在从微软官方站点下载安装程序……", 0));
            var download = new DirectProgress<DownloadProgress>(value => progress.Report(DownloadStatus(value)));
            installer = await delegates.Download(temporaryDirectory, download, token);
            token.ThrowIfCancellationRequested();
            progress.Report(new(PreparationStage.Verifying, "正在核对下载文件的 Windows 数字签名与发布者……", -1));
            var signature = await Task.Run(() => delegates.Verify(installer), token);
            if (!signature.Trusted) return new(PreparationOutcome.SignatureRejected, null, signature.Message);
            // A cancel request that arrived while the background signature check ran must
            // not reach the installer.
            token.ThrowIfCancellationRequested();
            progress.Report(new(PreparationStage.Installing, "正在安装 WebView2 运行组件……官方安装程序结束后会重新检测。", -1));
            int exitCode = await delegates.Install(installer);
            progress.Report(new(PreparationStage.Rechecking, "官方安装程序已结束，正在重新检测本机的 WebView2 运行组件……", -1));
            var after = delegates.Detect();
            if (after.State != WebViewRuntimeState.Present)
                return new(PreparationOutcome.StillMissing, null,
                    $"官方安装程序返回 {exitCode}，但本机仍未检测到 WebView2 运行组件，不会继续启动。");
            return new(PreparationOutcome.Prepared, after.Version, "WebView2 运行组件已就绪，正在继续启动。");
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            return new(PreparationOutcome.Cancelled, null, "已取消准备，未安装任何组件。");
        }
        catch (RuntimeDownloadException error)
        {
            return new(PreparationOutcome.DownloadFailed, null, error.Message);
        }
        catch (RuntimeInstallException error)
        {
            return new(PreparationOutcome.InstallFailed, null, error.Message);
        }
        catch (Exception)
        {
            return new(PreparationOutcome.Failed, null, "准备工作未能完成，请重试。");
        }
        finally
        {
            WebViewRuntime.RemoveInstaller(installer);
        }
    }

    static PreparationStatus DownloadStatus(DownloadProgress value)
    {
        long? total = value.Total;
        if (total is long known && known > 0)
        {
            int percent = (int)(value.Received * 100 / known);
            return new(PreparationStage.Downloading,
                $"正在从微软官方站点下载安装程序……{percent}%（{Megabytes(value.Received)} / {Megabytes(known)}）", percent);
        }
        return new(PreparationStage.Downloading,
            $"正在从微软官方站点下载安装程序……{Megabytes(value.Received)}", -1);
    }

    static string Megabytes(long bytes) => (bytes / 1024d / 1024d).ToString("0.0", CultureInfo.InvariantCulture) + " MB";
}

// A native preparation window: it must work precisely because WebView2 is unusable, so
// it uses no HTML, no page messages and no backend. It never runs during normal startup
// on a machine that already has the runtime.
internal sealed class WebViewRuntimeWindow : Form
{
    readonly string temporaryDirectory;
    readonly PreparationDelegates delegates;
    readonly Label heading = new()
    {
        Dock = DockStyle.Fill, AutoSize = false, Text = "需要准备 WebView2 运行组件",
        Font = new Font("Microsoft YaHei UI", 14F, FontStyle.Bold)
    };
    readonly Label explanation = new()
    {
        Dock = DockStyle.Fill, AutoSize = false, TextAlign = ContentAlignment.TopLeft
    };
    readonly Label statusText = new()
    {
        Dock = DockStyle.Fill, AutoSize = false, TextAlign = ContentAlignment.TopLeft, Text = "尚未开始准备。"
    };
    readonly ProgressBar progressBar = new()
    {
        Dock = DockStyle.Fill, Style = ProgressBarStyle.Continuous, Minimum = 0, Maximum = 100, Value = 0, Margin = new Padding(0, 6, 0, 6)
    };
    readonly Button prepareButton = new() { Text = "准备运行组件", AutoSize = true, MinimumSize = new Size(150, 34) };
    readonly Button cancelButton = new() { Text = "取消下载", AutoSize = true, MinimumSize = new Size(110, 34), Visible = false };
    readonly Button exitButton = new() { Text = "退出栖伴", AutoSize = true, MinimumSize = new Size(110, 34) };
    CancellationTokenSource? download;
    bool busy, installing, exitRequested, cancelling;

    internal PreparationResult? Outcome { get; private set; }

    WebViewRuntimeWindow(string temporaryDirectory, PreparationDelegates delegates, string? simulationNote)
    {
        this.temporaryDirectory = temporaryDirectory;
        this.delegates = delegates;
        Text = simulationNote is null ? "栖伴 Qiban · 准备 WebView2 运行组件" : "栖伴 Qiban · 组件准备模拟验证";
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoScaleDimensions = new SizeF(96F, 96F);
        ClientSize = new Size(660, 560);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Microsoft YaHei UI", 10F);
        string note = simulationNote is null ? "" : "\r\n\r\n" + simulationNote;
        explanation.Text = "栖伴需要 Microsoft Edge WebView2 来显示界面，本机尚未检测到该组件。\r\n\r\n"
            + "点击准备后将从微软下载并验证安装程序，再按当前用户安装。不会打开浏览器，也不会主动请求管理员权限。\r\n\r\n"
            + "下载可以取消；安装期间退出会等待安装结束或达到等待期限，不会强制中断安装。准备完成后将重新检测，再继续启动栖伴。"
            + note;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Padding = new Padding(20, 18, 20, 16) };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 62));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 52));
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false };
        buttons.Controls.Add(prepareButton);
        buttons.Controls.Add(cancelButton);
        buttons.Controls.Add(exitButton);
        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(explanation, 0, 1);
        layout.Controls.Add(statusText, 0, 2);
        layout.Controls.Add(progressBar, 0, 3);
        layout.Controls.Add(buttons, 0, 4);
        Controls.Add(layout);
        AcceptButton = prepareButton;
        CancelButton = exitButton;
        prepareButton.Click += async (_, _) => await StartAsync();
        cancelButton.Click += (_, _) => CancelDownload();
        exitButton.Click += (_, _) => RequestExit();
        FormClosing += (_, e) =>
        {
            if (!busy) return;
            e.Cancel = true;
            RequestExit();
        };
    }

    internal static PreparationResult? Show(string temporaryDirectory, PreparationDelegates delegates, string? simulationNote = null)
    {
        using var window = new WebViewRuntimeWindow(temporaryDirectory, delegates, simulationNote);
        window.ShowDialog();
        return window.Outcome;
    }

    void Apply(PreparationStatus value)
    {
        statusText.Text = value.Text;
        // Cancelling is a download-phase action: once verification/installation starts the
        // only remaining choice is to exit and wait for the installer to end.
        bool cancellable = value.Stage == PreparationStage.Downloading && !cancelling && !exitRequested;
        cancelButton.Visible = cancellable;
        cancelButton.Enabled = cancellable;
        if (value.Stage == PreparationStage.Installing) installing = true;
        if (value.Percent is >= 0 and <= 100)
        {
            progressBar.Style = ProgressBarStyle.Continuous;
            progressBar.Value = value.Percent;
        }
        else progressBar.Style = ProgressBarStyle.Marquee;
    }

    // Status and progress are applied inline on this window's thread. The flow resumes its
    // continuations on the same context, and a report coming from the download's background
    // work is marshalled synchronously, so a late download report cannot overwrite a later
    // stage or a final result.
    sealed class WindowProgress(WebViewRuntimeWindow window) : IProgress<PreparationStatus>
    {
        public void Report(PreparationStatus value)
        {
            if (window.IsDisposed || !window.IsHandleCreated) return;
            try
            {
                if (window.InvokeRequired) window.Invoke(new Action(() => window.Apply(value)));
                else window.Apply(value);
            }
            catch (Exception error) when (error is ObjectDisposedException or InvalidOperationException) { }
        }
    }

    void CancelDownload()
    {
        cancelling = true;
        cancelButton.Enabled = false;
        statusText.Text = "正在取消下载……";
        download?.Cancel();
    }

    // Closing during installation waits for the official installer instead of ending it.
    void RequestExit()
    {
        if (!busy) { Close(); return; }
        exitRequested = true;
        prepareButton.Enabled = false;
        cancelButton.Visible = false;
        exitButton.Enabled = false;
        if (installing)
        {
            statusText.Text = "正在安装 WebView2 运行组件……退出会等到官方安装程序结束或等待期限到达后再进行，不会中断或强制结束安装。";
            return;
        }
        statusText.Text = "正在取消下载并退出……";
        download?.Cancel();
    }

    async Task StartAsync()
    {
        if (busy) return;
        busy = true;
        installing = false;
        exitRequested = false;
        cancelling = false;
        download = new CancellationTokenSource();
        prepareButton.Enabled = false;
        prepareButton.Text = "准备运行组件";
        cancelButton.Visible = true;
        cancelButton.Enabled = true;
        exitButton.Enabled = true;
        progressBar.Style = ProgressBarStyle.Marquee;
        statusText.Text = "正在准备 WebView2 运行组件……";
        PreparationResult result;
        try
        {
            result = await WebViewRuntimePreparation.RunAsync(delegates, temporaryDirectory, new WindowProgress(this), download.Token);
        }
        catch (Exception)
        {
            result = new PreparationResult(PreparationOutcome.Failed, null, "准备工作未能完成，请重试。");
        }
        finally
        {
            busy = false;
            installing = false;
            download.Dispose();
            download = null;
        }
        if (exitRequested) { Outcome = null; Close(); return; }
        Outcome = result;
        if (result.Prepared)
        {
            progressBar.Style = ProgressBarStyle.Continuous;
            progressBar.Value = 100;
            statusText.Text = result.Message;
            Close();
            return;
        }
        ShowFailure(result);
    }

    void ShowFailure(PreparationResult result)
    {
        statusText.Text = result.Message + "\r\n重试会先重新检测本机：如果安装其实已经完成，将直接继续启动。";
        prepareButton.Text = "重试";
        prepareButton.Enabled = true;
        cancelling = false;
        cancelButton.Visible = false;
        exitButton.Enabled = true;
        progressBar.Style = ProgressBarStyle.Continuous;
    }
}
