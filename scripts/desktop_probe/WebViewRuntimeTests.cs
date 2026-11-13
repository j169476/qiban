// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// Acceptance only. The preparation flow is exercised through injected detect/download/
// verify/install delegates, so no case needs a real network request or a real installer.
internal static class WebViewRuntimeTests
{
    internal static int Run(string directory)
    {
        Directory.CreateDirectory(directory);
        var cases = new List<string>();
        try
        {
            // 1. An installed runtime costs nothing: no download, no signature check, no installer.
            {
                int downloads = 0, verifies = 0, installs = 0;
                var delegates = Delegates(
                    () => new WebViewRuntimeDetection(WebViewRuntimeState.Present, "140.0.3485.44", "runtime_reported"),
                    (_, token) => { downloads++; return Task.FromResult("unused"); },
                    _ => { verifies++; return SignatureVerdict.MicrosoftPublisher; },
                    _ => { installs++; return Task.FromResult(0); });
                var result = Execute(delegates, directory, CancellationToken.None);
                Require(result is { Prepared: true, Version: "140.0.3485.44" }, "existing_runtime_is_used");
                Require(downloads == 0 && verifies == 0 && installs == 0, "existing_runtime_skips_download");
                cases.Add("existing_runtime_is_used_without_download_or_installer");
            }
            // 2. A missing runtime is prepared once and detection runs again before success.
            {
                int detects = 0, verifies = 0, installs = 0;
                var stages = new StageRecorder();
                var delegates = Delegates(
                    () => ++detects == 1
                        ? new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found")
                        : new WebViewRuntimeDetection(WebViewRuntimeState.Present, "140.0.3485.44", "runtime_reported"),
                    (_, token) => Task.FromResult(Path.Combine(directory, "MicrosoftEdgeWebview2Setup.exe")),
                    _ => { verifies++; return SignatureVerdict.MicrosoftPublisher; },
                    _ => { installs++; return Task.FromResult(0); });
                var result = Execute(delegates, directory, CancellationToken.None, stages);
                Require(result is { Prepared: true, Version: "140.0.3485.44" }, "missing_runtime_prepared");
                Require(detects == 2 && verifies == 1 && installs == 1, "missing_runtime_single_install_and_recheck");
                Require(stages.Contains(PreparationStage.Downloading) && stages.Contains(PreparationStage.Verifying)
                    && stages.Contains(PreparationStage.Installing) && stages.Contains(PreparationStage.Rechecking), "missing_runtime_reports_progress");
                cases.Add("missing_runtime_prepares_then_rechecks_detection");
            }
            // 3. A failed download never reaches verification or installation and can be retried.
            {
                int verifies = 0, installs = 0;
                var failing = Delegates(
                    () => new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found"),
                    (_, _) => Task.FromException<string>(new RuntimeDownloadException("无法连接微软下载站点，请检查网络后重试。")),
                    _ => { verifies++; return SignatureVerdict.MicrosoftPublisher; },
                    _ => { installs++; return Task.FromResult(0); });
                var failed = Execute(failing, directory, CancellationToken.None);
                Require(failed.Outcome == PreparationOutcome.DownloadFailed, "download_failure_reported");
                Require(verifies == 0 && installs == 0, "download_failure_never_installs");
                int detects = 0, retryInstalls = 0;
                var working = Delegates(
                    () => ++detects == 1
                        ? new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found")
                        : new WebViewRuntimeDetection(WebViewRuntimeState.Present, "140.0.3485.44", "runtime_reported"),
                    (_, _) => Task.FromResult(Path.Combine(directory, "MicrosoftEdgeWebview2Setup.exe")),
                    _ => SignatureVerdict.MicrosoftPublisher,
                    _ => { retryInstalls++; return Task.FromResult(0); });
                var retried = Execute(working, directory, CancellationToken.None);
                Require(retried.Prepared && retryInstalls == 1, "download_failure_retry_succeeds");
                cases.Add("download_failure_is_retryable_and_never_installs");
            }
            // 4. A file that fails Windows signature or publisher verification is never executed.
            {
                int installs = 0;
                var delegates = Delegates(
                    () => new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found"),
                    (_, _) => Task.FromResult(Path.Combine(directory, "MicrosoftEdgeWebview2Setup.exe")),
                    _ => SignatureVerdict.Rejection("下载文件的签名发布者不是 Microsoft Corporation，已丢弃且不会运行。"),
                    _ => { installs++; return Task.FromResult(0); });
                var result = Execute(delegates, directory, CancellationToken.None);
                Require(result.Outcome == PreparationOutcome.SignatureRejected, "signature_rejection_reported");
                Require(installs == 0, "signature_rejection_never_executes");
                // The same case also runs the real Windows verification once, on a temporary
                // file that was never downloaded from anywhere and is never executed.
                string unsigned = Path.Combine(directory, "unsigned-signature-probe.bin");
                File.WriteAllBytes(unsigned, [0x4D, 0x5A, 0x00, 0x00, 0x51, 0x49, 0x42, 0x41, 0x4E]);
                Require(!WebViewRuntime.VerifySignature(unsigned).Trusted, "real_verification_rejects_unsigned_file");
                cases.Add("untrusted_or_non_microsoft_signature_is_not_executed");
            }
            // 5. A cancelled download stops before verification and installation.
            {
                int verifies = 0, installs = 0;
                using var source = new CancellationTokenSource(TimeSpan.FromMilliseconds(80));
                var delegates = Delegates(
                    () => new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found"),
                    async (_, token) => { await Task.Delay(Timeout.InfiniteTimeSpan, token); return "unused"; },
                    _ => { verifies++; return SignatureVerdict.MicrosoftPublisher; },
                    _ => { installs++; return Task.FromResult(0); });
                var result = Execute(delegates, directory, source.Token);
                Require(result.Outcome == PreparationOutcome.Cancelled, "cancel_reported");
                Require(verifies == 0 && installs == 0, "cancel_never_installs");
                cases.Add("cancelled_download_stops_before_verification_and_install");
            }
            // 6. An installer exit code of zero is not evidence: detection must now pass.
            {
                int detects = 0, installs = 0;
                var delegates = Delegates(
                    () => { detects++; return new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "runtime_not_found"); },
                    (_, _) => Task.FromResult(Path.Combine(directory, "MicrosoftEdgeWebview2Setup.exe")),
                    _ => SignatureVerdict.MicrosoftPublisher,
                    _ => { installs++; return Task.FromResult(0); });
                var result = Execute(delegates, directory, CancellationToken.None);
                Require(installs == 1 && detects == 2, "exit_zero_installer_ran_once");
                Require(result.Outcome == PreparationOutcome.StillMissing && result.Version is null, "exit_zero_without_runtime_is_not_prepared");
                cases.Add("installer_exit_zero_without_runtime_does_not_continue");
            }
            File.WriteAllText(Path.Combine(directory, "webview-runtime-result.json"),
                JsonSerializer.Serialize(new { passed = true, cases }));
            return 0;
        }
        catch (Exception error)
        {
            File.WriteAllText(Path.Combine(directory, "webview-runtime-result.json"),
                JsonSerializer.Serialize(new { passed = false, cases, error = error.Message }));
            return 1;
        }
    }

    static void Require(bool value, string name)
    {
        if (!value) throw new Exception("webview_runtime_assertion_failed:" + name);
    }

    static PreparationDelegates Delegates(
        Func<WebViewRuntimeDetection> detect,
        Func<IProgress<DownloadProgress>, CancellationToken, Task<string>> download,
        Func<string, SignatureVerdict> verify,
        Func<string, Task<int>> install)
        => new(detect, (directory, progress, token) => download(progress, token), verify, install);

    static PreparationResult Execute(PreparationDelegates delegates, string directory, CancellationToken token,
        StageRecorder? stages = null)
        => WebViewRuntimePreparation.RunAsync(delegates, directory, stages ?? new StageRecorder(), token)
            .GetAwaiter().GetResult();

    sealed class StageRecorder : IProgress<PreparationStatus>
    {
        readonly List<PreparationStage> reported = [];
        internal bool Contains(PreparationStage stage) { lock (reported) return reported.Contains(stage); }
        public void Report(PreparationStatus value) { lock (reported) reported.Add(value.Stage); }
    }
}

// Diagnostic entry for acceptance only: --webview-signature-check <file> <report> runs the
// real Windows signature and publisher verification on one file and writes the verdict.
// The file is only read by WinTrust; it is never started.
internal static class WebViewRuntimeDiagnostics
{
    internal static int SignatureCheck(string file, string report)
    {
        SignatureVerdict verdict;
        try { verdict = WebViewRuntime.VerifySignature(file); }
        catch (Exception error) { verdict = SignatureVerdict.Rejection("签名检查未能完成：" + error.GetType().Name); }
        string full = Path.GetFullPath(report);
        string? parent = Path.GetDirectoryName(full);
        if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);
        File.WriteAllText(full, JsonSerializer.Serialize(new
        {
            file = Path.GetFileName(file),
            trusted = verdict.Trusted,
            message = verdict.Message
        }));
        return verdict.Trusted ? 0 : 1;
    }
}

// Dedicated native fault-injection entry for acceptance only: --webview-runtime-inspect
// opens the real missing-runtime window with injected delegates, so the root CUA can see
// the actual UI. Nothing is downloaded, verified on disk or installed.
internal static class WebViewRuntimeInspect
{
    internal static int Run(string[] args)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string fault = "success";
        int index = Array.IndexOf(args, "--webview-runtime-inspect-fault");
        if (index >= 0 && index + 1 < args.Length) fault = args[index + 1];
        _ = WebViewRuntimeWindow.Show(Path.Combine(Path.GetTempPath(), "qiban-webview-runtime-inspect"),
            Simulated(fault), "验收模拟：本窗口不会下载、校验或安装任何真实文件。");
        return 0;
    }

    internal static PreparationDelegates Simulated(string fault)
    {
        bool installed = false;
        bool downloadFailure = string.Equals(fault, "download", StringComparison.OrdinalIgnoreCase);
        bool signatureFailure = string.Equals(fault, "signature", StringComparison.OrdinalIgnoreCase);
        bool runtimeMissingAfterInstall = string.Equals(fault, "missing", StringComparison.OrdinalIgnoreCase);
        bool slowDownload = string.Equals(fault, "cancel", StringComparison.OrdinalIgnoreCase);
        return new PreparationDelegates(
            () => installed
                ? new WebViewRuntimeDetection(WebViewRuntimeState.Present, "140.0.3485.44（模拟）", "simulated_installed")
                : new WebViewRuntimeDetection(WebViewRuntimeState.Missing, null, "simulated_missing"),
            async (directory, progress, token) =>
            {
                const long total = 2_400_000;
                int steps = slowDownload ? 200 : 40;
                for (int step = 1; step <= steps; step++)
                {
                    if (downloadFailure && step == steps / 3)
                        throw new RuntimeDownloadException("模拟：无法连接微软下载站点，请检查网络后重试。");
                    await Task.Delay(slowDownload ? 200 : 60, token);
                    progress.Report(new DownloadProgress(total * step / steps, total));
                }
                return Path.Combine(directory, "模拟-未真实下载.exe");
            },
            _ => signatureFailure
                ? SignatureVerdict.Rejection("模拟：下载文件的签名发布者不是 Microsoft Corporation，已丢弃且不会运行。")
                : SignatureVerdict.MicrosoftPublisher,
            async _ =>
            {
                await Task.Delay(1500);
                if (!runtimeMissingAfterInstall) installed = true;
                return 0;
            });
    }
}
