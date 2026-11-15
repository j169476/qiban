// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Qiban.DesktopProbe;

internal static class Program
{
    // INSTALL-174: one constant marker that only says "a Qiban GUI process is alive". It is created
    // without ownership and never waited on, so it neither serializes profiles nor children; the
    // installer's AppMutex and the uninstaller's InitializeUninstall check this exact name and ask
    // the person to exit from inside the window instead of force-closing the application.
    internal const string RunningMarker = @"Local\QibanDesktopApplicationRunning";
    internal static AppPaths Paths = null!;
    internal static string Data => Paths.Root;
    internal static string Instance = "";
    internal static string? StackSpecPath;
    internal static string? PostgresSpecPath;
    internal static bool ManagedInstall;
    internal static bool FailureSmoke;
    internal static bool Smoke;
    internal static bool LifecycleWindow;
    internal static bool RecoveryInspect;
    internal static bool RestoreInspect;
    internal static bool ModelInspect;
    internal static bool ModelCancelInspect;
    internal static bool ModelCancelSmoke;
    internal static bool AccountPickerInspect;
    internal static bool AccountPickerSmoke;
    internal static bool QQInspect;
    internal static string? QQTargetsInspect;
    internal static bool QQRuntimeInspect;
    internal static bool QQDirectoryInspect;
    internal static bool ShutdownInspect;
    internal static bool ShutdownSmoke;
    internal static bool RecoveryCheckSmoke;
    // This process is the uninstall preparation page (started by uninstall.exe or from the
    // window's own "卸载栖伴" entry). Every child it starts is a preparation backend: no model, no
    // platform connector and no automatic run.
    internal static bool UninstallPrepare;
    internal static bool BackupOnly;
    internal static bool UninstallSmoke;
    [STAThread]
    static int Main(string[] args)
    {
        // Two fixed internal modes and the published alias are decided before anything else, so
        // neither of them can ever reach the window, a profile or a database. A helper waits for
        // exactly one process id and then performs one fixed action from its own directory.
        if(args.Length==1 && args[0]==ProfileCleanup.Switch)return ProfileCleanup.Run();
        if(args.Length==2 && UninstallRule.IsHelper(args[0]) && int.TryParse(args[1],out int waited))
            return UninstallHelper.Run(args[0],waited);
        if(args.Length==3 && args[0]==UninstallRule.OpenPreparationHelper && int.TryParse(args[1],out int profileWait))
            return UninstallHelper.Run(args[0],profileWait,Path.GetFullPath(args[2]));
        if(UninstallRule.IsAlias(Path.GetFileName(Environment.ProcessPath)))
            return UninstallRule.StartPreparationPage(Path.GetDirectoryName(Environment.ProcessPath!)!) ? 0 : 1;
        if(args.Length==2 && args[0]=="--shutdown-tests")return ShutdownTests.Run(args[1]);
        if(args.Length==2 && args[0]=="--uninstall-tests")return UninstallTests.Run(args[1]);
        if(args.Length==2 && args[0]=="--webview-runtime-tests")return WebViewRuntimeTests.Run(args[1]);
        if(args.Length==3 && args[0]=="--webview-signature-check")return WebViewRuntimeDiagnostics.SignatureCheck(args[1],args[2]);
        if(args.Contains("--webview-runtime-inspect"))return WebViewRuntimeInspect.Run(args);
        if(args.Length==4 && args[0]=="--account-reopen-tests")return AccountReopenTests.Run(args[1],args[2],args[3]).GetAwaiter().GetResult();
        if(args.Length==4 && args[0]=="--recovery-reopen-tests")return RecoveryReopenTests.Run(args[1],args[2],args[3]).GetAwaiter().GetResult();
        if(args.Length==2 && args[0]=="--pipe-tests")return PipeTests.Run(args[1]).GetAwaiter().GetResult();
        if(args.Length==2 && args[0]=="--portable-tests")return PortableTransferTests.Verify(args[1]).GetAwaiter().GetResult();
        if(args.Length==2 && args[0]=="--windows-verification-status")
        {File.WriteAllText(args[1],JsonSerializer.Serialize(new{availability=WindowsVerification.Availability().GetAwaiter().GetResult()}));return 0;}
        if(args.Length==4 && args[0]=="--management-tests")
        {
            try
            {
                // The portable transfer battery needs no database, so it runs first:
                // a failure there must not be hidden behind the rest of the battery.
                int portable=PortableTransferTests.Verify(args[3]).GetAwaiter().GetResult();
                int managed=ManagementTests.Run(args[1],args[2],args[3]).GetAwaiter().GetResult();
                return portable!=0||managed!=0?1:0;
            }
            catch(Exception error)
            {
                // Automated checks report failure to their runner, not a Windows
                // unhandled-exception dialog which waits for a person to close it.
                Console.Error.WriteLine("management_test_failed:"+error.GetType().Name);
                return 1;
            }
        }
        if(args.Length==2 && args[0]=="--management-impostor")return ManagementTests.Impostor(args[1]).GetAwaiter().GetResult();
        if (args.Length == 3 && args[0] == "--probe-service") return ProbeService.Run(args[1], args[2]);
        if (args.Length == 2 && args[0] == "--group-tests") return GroupTests.Run(args[1]).GetAwaiter().GetResult();
        if (args.Length == 2 && args[0] == "--component-tests") return ComponentTests.Run(args[1]).GetAwaiter().GetResult();
        if (args.Length == 2 && args[0] == "--upgrade-confirm-tests") return UpgradeConfirmTests.Run(args[1]).GetAwaiter().GetResult();
        if (args.Length == 2 && args[0] == "--upgrade-recovery-tests") return UpgradeRecoveryTests.Run(args[1]).GetAwaiter().GetResult();
        if (args.Length > 0 && args[0].StartsWith("--lifecycle-", StringComparison.Ordinal) && args[0] != "--lifecycle-window")
            return LifecycleProbe.Run(args);
        if (args.Length == 4 && args[0] == "--postgres-worker") return PostgresComponent.Run(args[1],args[2],int.Parse(args[3])).GetAwaiter().GetResult();
        if ((args.Length == 3 || args.Length == 4) && args[0] == "--postgres-host")
        {
            int seconds=60;
            if(args.Length==4 && !int.TryParse(args[3],out seconds))return 2;
            try{return PostgresComponent.Host(args[1],args[2],seconds).GetAwaiter().GetResult();}
            catch(Exception error){Console.Error.WriteLine("database_test_host_failed:"+error.GetType().Name);return 1;}
        }
        if(args.Length==4 && args[0]=="--storage-worker")return StorageComponent.Run(args[1],args[2],int.Parse(args[3])).GetAwaiter().GetResult();
        // The uninstall preparation backend: the same worker, without a model, a platform connector,
        // the scheduler or any automatic run (see StorageComponent.BackupOnlyArgument).
        if(args.Length==5 && args[0]=="--storage-worker" && args[4]==StorageComponent.BackupOnlyArgument)
            return StorageComponent.Run(args[1],args[2],int.Parse(args[3]),backupOnly:true).GetAwaiter().GetResult();
        if(args.Length==3 && args[0]=="--stack-host")return StorageComponent.Host(args[1],args[2]).GetAwaiter().GetResult();
        if(args.Length==6 && args[0]=="--installation-worker")return InstallationComponent.Run(args[1],args[2],args[3],args[4],int.Parse(args[5])).GetAwaiter().GetResult();
        if(args.Length==5 && args[0]=="--install-host")return InstallationComponent.Host(args[1],args[2],args[3],args[4]).GetAwaiter().GetResult();
        if(args.Length==4 && args[0]=="--upgrade-confirm-worker")return UpgradeConfirmComponent.Run(args[1],args[2],int.Parse(args[3])).GetAwaiter().GetResult();
        // The rollback window's own bridge: it runs inside the window's Job, receives the Windows
        // verification receipt on a current-user-only pipe from its parent, and feeds one redirected
        // backend with it. Nothing else starts it (see UpgradeRecoveryHost).
        if(args.Length==7 && args[0]==UpgradeRecoveryHost.BridgeMode)
            return UpgradeRecoveryHost.Bridge(args[1],args[2],args[3],args[4],args[5],int.Parse(args[6])).GetAwaiter().GetResult();
        // UPGRADE-177 rollback window. It is dispatched before the ordinary running marker exists,
        // because a rollback is refused while that marker is held and this window must never start
        // business, the database or the WebView. It runs from the transaction's carrier copy; a
        // program started anywhere else only hands the request over to that carrier (see
        // UpgradeRecovery.Run) instead of running maintenance from the directory being replaced.
        if(args.Length==3 && args[0]=="--upgrade-recovery")return UpgradeRecovery.Run(args[1],args[2]);
        // The preparation page never coexists with a running application: the installer's own
        // uninstaller refuses to run while the marker exists, and this program never ends that
        // process for it. The person exits normally from inside the window and reopens the entry.
        UninstallPrepare=args.Contains(UninstallRule.PreparationSwitch);
        UninstallSmoke=UninstallPrepare && args.Contains("--uninstall-smoke") && args.Contains("--data-dir");
        if(UninstallPrepare && Mutex.TryOpenExisting(RunningMarker,out Mutex? running))
        {
            running.Dispose();
            MessageBox.Show(@"栖伴正在运行，因此现在不能打开卸载准备。" + "\n\n"
                + "请先回到栖伴窗口，用界面或托盘中的正常退出流程关闭栖伴"
                + "（不要强制结束进程，也不要停止数据库），然后再打开卸载入口。" + "\n\n"
                + "卸载没有开始，程序和资料都保持原样。", "栖伴 · 卸载准备",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }
        // Only the normal GUI entry holds the running marker: the automated test and worker modes
        // above never create it, and the per-profile activation mutex below still separates profiles.
        using Mutex runningMarker = new(false, RunningMarker);
#if QIBAN_MANAGED_APPLICATION
        ManagedInstall=true;
#else
        ManagedInstall=args.Contains("--managed-install");
#endif
        int stackIndex=Array.IndexOf(args,"--stack-spec");
        StackSpecPath=stackIndex>=0?Path.GetFullPath(args[stackIndex+1]):null;
        int postgresIndex = Array.IndexOf(args, "--postgres-spec");
        PostgresSpecPath = postgresIndex>=0 ? Path.GetFullPath(args[postgresIndex+1]) : null;
        FailureSmoke = args.Contains("--database-failure-smoke");
        Smoke = args.Contains("--smoke") || FailureSmoke;
        LifecycleWindow = args.Contains("--lifecycle-window");
        RecoveryInspect=args.Contains("--recovery-inspect");
        RestoreInspect=args.Contains("--restore-inspect");
        ModelInspect=args.Contains("--model-inspect");
        ModelCancelInspect=args.Contains("--model-cancel-inspect");
        ModelCancelSmoke=args.Contains("--model-cancel-smoke");
        AccountPickerInspect=args.Contains("--account-picker-inspect");
        AccountPickerSmoke=args.Contains("--account-picker-smoke");
        QQInspect=args.Contains("--qq-inspect");
        QQRuntimeInspect=args.Contains("--qq-runtime-inspect");
        QQDirectoryInspect=args.Contains("--qq-directory-inspect");
        ShutdownInspect=args.Contains("--shutdown-inspect") || args.Contains("--shutdown-smoke");
        ShutdownSmoke=args.Contains("--shutdown-smoke");
        RecoveryCheckSmoke=args.Contains("--recovery-check-smoke");
        int qqTargetsIndex=Array.IndexOf(args,"--qq-targets-inspect");
        QQTargetsInspect=qqTargetsIndex>=0?Path.GetFullPath(args[qqTargetsIndex+1]):null;
        int index = Array.IndexOf(args, "--data-dir");
        Paths = new AppPaths(index >= 0 ? args[index + 1] : null);
        Paths.Prepare();
        Instance = "QibanDesktopProbe-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
            Environment.UserName + "|" + Data.ToUpperInvariant())))[..24];
        using var activate = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\" + Instance + "-activate");
        using var mutex = new Mutex(true, "Local\\" + Instance, out bool first);
        if (!first) { activate.Set(); return 0; }
        try
        {
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            // R1.5／R7.5: WebView2 is the window's only hard dependency. A missing runtime is
            // prepared natively before the main window exists, so the journal entry, event
            // wiring and backend start below still happen exactly once. An installed runtime
            // returns here without a network request; every other startup failure keeps its
            // original text and is never repaired automatically.
            if(!WebViewRuntime.EnsureReady(Program.Paths.Temporary))return 0;
            using var window = new ProbeWindow(activate);
            Application.Run(window);
            return window.Result;
        }
        finally { mutex.ReleaseMutex(); }
    }
}

internal sealed class ProbeWindow : Form
{
    const string Origin = "https://qiban.internal";
    readonly WebView2 view = new() { Dock = DockStyle.Fill };
    readonly Label fallback = new() { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
        Text = "正在准备栖伴窗口……", Font = new Font("Microsoft YaHei UI", 14) };
    readonly NotifyIcon tray = new() { Icon = SystemIcons.Application, Text = "栖伴 Qiban", Visible = true };
    readonly System.Windows.Forms.Timer poll = new() { Interval = 500 };
    readonly CancellationTokenSource lifetime = new();
    readonly Stopwatch clock = Stopwatch.StartNew();
    readonly EventWaitHandle activate;
    readonly string pipeName = Program.Instance + "-service";
    readonly List<object> events = [];
    readonly ComponentGroup component;
    readonly ShutdownJournal journal = new(Program.Paths.Data);
    readonly Panel shutdownPanel = new() { Dock=DockStyle.Fill,Visible=false };
    readonly Label shutdownText = new() { Dock=DockStyle.Fill,TextAlign=ContentAlignment.MiddleCenter,Font=new Font("Microsoft YaHei UI",14) };
    readonly Button forceExit = new() { Dock=DockStyle.Bottom,Height=65,Text="强制退出",Visible=false };
    readonly Label recoveryStatus = new() { Dock=DockStyle.Top,Height=65,Visible=false,TextAlign=ContentAlignment.MiddleLeft };
    readonly RecoveryNotice recoveryNotice = new();
    bool journalStarted;
    Process? backend => component.Process;
    bool exiting, shutdownComplete, checking, loaded;
    // Set before Exit when this run must, after a successful shutdown only, let one fixed internal
    // helper reopen the preparation page or start the registered uninstaller.
    string? helperMode;
    UninstallSession? preparation;
    int activations;
    readonly SemaphoreSlim control = new(1,1);
    long controlEpoch;
    bool recovering;
    // Portable transfer keeps its own flags so an in-flight switch is never mistaken
    // for a lost backend and a second transfer can never start.
    bool portableBusy, portableActive, portablePending;
    internal int Result = 0;
    readonly Dictionary<string, string> resources = new()
    {
        ["/"]="index.html", ["/manual"]="manual.html", ["/uninstall"]="uninstall.html",
        ["/uninstall.js"]="uninstall.js", ["/feedback.js"]="feedback.js", ["/dragon-rest.svg"]="dragon-rest.svg",
        ["/probe.css"]="probe.css", ["/probe.js"]="probe.js",
        ["/management.js"]="management.js",["/room.js"]="room.js",["/tasks.js"]="tasks.js",["/model.js"]="model.js",["/bili.js"]="bili.js",["/qq.js"]="qq.js",["/audience.js"]="audience.js",["/reply_quota.js"]="reply_quota.js",["/reply_trace.js"]="reply_trace.js",["/media.js"]="media.js",["/stickers.js"]="stickers.js",["/files.js"]="files.js",["/growth.js"]="growth.js",["/memories.js"]="memories.js",["/baseline.js"]="baseline.js",["/relationship.js"]="relationship.js",["/sharing.js"]="sharing.js",["/relation-claims.js"]="relation-claims.js",["/navigation.js"]="navigation.js",["/static/app.css"]="app.css", ["/static/qiban.css"]="qiban.css",
        ["/static/white-dragon-tea.png"]="white-dragon-tea.png"
    };
    public ProbeWindow(EventWaitHandle activation)
    {
        activate = activation;
        component = Program.ShutdownInspect ? new ComponentGroup(ProbeService.Definition(pipeName,"ignore-stop") with {WaitForExplicitForce=true})
            : Program.ManagedInstall ? InstallationComponent.Group(Path.Combine(Program.Paths.Data,"installation"),AppContext.BaseDirectory,pipeName)
            : Program.StackSpecPath is not null ? StorageComponent.CreateStack(Program.StackSpecPath,pipeName)
            : new ComponentGroup(Program.PostgresSpecPath is null ? ProbeService.Definition(pipeName) : PostgresComponent.Definition(Program.PostgresSpecPath,pipeName));
        component.Changed += () =>
        {
            Log("component_state", new { id=component.Id, state=component.State, detail=component.Detail, members=component.Snapshot });
            PublishService();
            if(journalStarted && component.State=="failed")
            {
                journal.Abnormal();
                if(journal.NeedsRecovery)
                {
                    recoveryNotice.Failed();
                    recoveryStatus.Text="后台检查未通过，请查看诊断；现有资料已保留。";
                    recoveryStatus.Visible=true;
                }
            }
        };
        Text = "栖伴 Qiban";
        if(Program.UninstallPrepare)
        {
            // One session for this preparation run. Nothing is started, backed up or removed here:
            // the page asks for each step, and only a registered uninstaller can ever be started.
            Text = "栖伴 · 卸载准备";
            Program.BackupOnly = true;
            bool ready = Program.ManagedInstall && File.Exists(Path.Combine(Program.Paths.Data,"installation","installation.json"));
            preparation = new UninstallSession(Program.Paths.ApplicationDirectory, Program.Paths.Root,
                UninstallRule.ResolveUninstaller(Program.Paths.ApplicationDirectory, UninstallRule.RegisteredUninstaller),
                ready, ready ? "" : "本机资料目录里还没有已就绪的安装，因此没有可备份的资料。");
        }
        if(Program.ShutdownInspect)Text="栖伴 · 退出流程验证（合成进程，无真实资料）";
        ClientSize = new Size(1080, 850); MinimumSize = new Size(560, 500);
        StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(fallback); Controls.Add(view); view.Visible = false;
        shutdownPanel.Controls.Add(shutdownText);shutdownPanel.Controls.Add(forceExit);
        Controls.Add(shutdownPanel);Controls.Add(recoveryStatus);
        forceExit.Click+=(_,_)=>ForceExit();
        var menu = new ContextMenuStrip();
        menu.Items.Add("打开栖伴", null, (_, _) => RestoreWindow());
        menu.Items.Add("退出栖伴", null, (_, _) => Exit());
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += (_, _) => RestoreWindow();
        FormClosing += (_, e) =>
        {
            if(Program.UninstallPrepare && !exiting && e.CloseReason==CloseReason.UserClosing)
            {e.Cancel=true;Exit();return;}
            if (!exiting && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel=true;
                using var choice=new CloseChoiceDialog();
                var result=choice.ShowDialog(this);
                if(result==DialogResult.OK)Exit();
                else if(result==DialogResult.Ignore)HideToTray();
            }
            else if (!shutdownComplete) { e.Cancel = true; Exit(); }
        };
        poll.Tick += async (_, _) =>
        {
            await CheckService();
            if(recoveryNotice.ShouldDismiss(clock.ElapsedMilliseconds, component.State=="ready"))
                recoveryStatus.Visible=false;
        };
        Shown += async (_, _) => await Initialize();
    }
    void Log(string name, object? detail = null)
    {
        var item = new { name, milliseconds = clock.ElapsedMilliseconds, detail };
        events.Add(item);
        File.AppendAllText(Path.Combine(Program.Paths.Logs, "events.jsonl"), JsonSerializer.Serialize(item) + "\n");
    }
    void HideToTray() {Hide();Log("hidden_to_tray");}
    void RestoreWindow() { Show(); WindowState = FormWindowState.Normal; Activate(); activations++; Log("window_activated"); }
    async Task Initialize()
    {
        try
        {
            journal.Begin();journalStarted=true;
            if(journal.NeedsRecovery)
            {
                recoveryNotice.Checking();
                recoveryStatus.Text="上次未正常退出，正在检查数据库恢复、资料密钥和关键资料……";
                recoveryStatus.Visible=true;recoveryStatus.BringToFront();
                Log("recovery_check_started");
            }
            Log("app_paths", Program.Paths.Describe());
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: Program.Paths.WebView);
            await view.EnsureCoreWebView2Async(environment);
            var core = view.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false; core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreHostObjectsAllowed = false; core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = true;
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += (_, e) => Serve(environment, e);
            core.NavigationStarting += (_, e) => { if (!Allowed(e.Uri)) { e.Cancel = true; Log("navigation_blocked"); } };
            core.NewWindowRequested += (_, e) => { e.Handled = true; Log("popup_blocked"); };
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.WebMessageReceived += async (_, e) => await Receive(e);
            core.NavigationCompleted += async (_, e) =>
            {
                if (!e.IsSuccess) { fallback.Text = "页面未能加载，请退出后重新打开。"; fallback.BringToFront(); return; }
                loaded = true; await CheckService();
                if(Program.UninstallPrepare)PostUninstall();
            };
            core.ProcessFailed += (_, e) => { Log("renderer_failed", e.ProcessFailedKind.ToString()); fallback.Text="页面暂时不可用，请退出后重新打开。";fallback.BringToFront(); };
            view.Visible = true; view.BringToFront(); core.Navigate(Origin + (Program.UninstallPrepare ? "/uninstall" : "/"));
            if(recoveryStatus.Visible)recoveryStatus.BringToFront();
            Log("webview_ready", new { version=environment.BrowserVersionString, dpi=DeviceDpi });
            poll.Start();
            _ = Task.Run(() =>
            {
                while (WaitHandle.WaitAny([activate, lifetime.Token.WaitHandle]) == 0)
                    if (!IsDisposed && IsHandleCreated) BeginInvoke(RestoreWindow);
            });
            if(Program.UninstallPrepare)
            {
                // The preparation page starts nothing by itself: the backend runs only when the
                // person asks for a backup, and no platform or model is ever opened here.
                Log("uninstall_preparation_ready", new {registered=preparation!.Registered,
                    backup_available=preparation.BackupAvailable});
                if(Program.UninstallSmoke)await UninstallBackupSmoke();
                return;
            }
            if(Program.ShutdownInspect)
            {
                await StartBackend();
                if(component.State!="ready")throw new IOException("shutdown_probe_not_ready");
                Exit();
                if(Program.ShutdownSmoke)
                {
                    await Until(()=>Task.FromResult(forceExit.Visible),15000);
                    if(backend is null || backend.HasExited || component.State!="stopping")throw new IOException("shutdown_wait_process_lost");
                    using var picture=new Bitmap(shutdownPanel.Width,shutdownPanel.Height);
                    shutdownPanel.DrawToBitmap(picture,shutdownPanel.ClientRectangle);picture.Save(Path.Combine(Program.Data,"shutdown-wait.png"));
                    Log("check_shutdown_wait_preserves_process_pass");ForceExit();
                }
                return;
            }
            if (Program.LifecycleWindow)
            {
                await StartBackend();
                await Until(()=>JsBool("document.body.dataset.service==='ready'"));
                Log("lifecycle_window_ready", new {pid=Environment.ProcessId, child=backend!.Id, browser=core.BrowserProcessId});
            }
            if (Program.Smoke) await Smoke();
            else if(Program.ManagedInstall)
            {
                await StartBackend();
                if(Program.RecoveryCheckSmoke)
                {
                    if(component.State!="ready")throw new IOException("recovery_check_not_ready");
                    Log("check_recovery_only_ready_pass");Exit();return;
                }
                if(Program.RecoveryInspect)await InspectRecovery();
                if(Program.RestoreInspect)await InspectRestore();
                if(Program.ModelInspect)await InspectModel();
                if(Program.ModelCancelInspect)await InspectModelCancel();
                if(Program.ModelCancelSmoke)await ModelCancellationSmoke();
                if(Program.AccountPickerInspect||Program.AccountPickerSmoke)await InspectAccountPicker();
                if(Program.QQInspect)await InspectQQ();
                if(Environment.GetCommandLineArgs().Contains("--bili-qr-handoff"))await BiliQrHandoff();
                if(Program.QQTargetsInspect is not null)await InspectQQTargets();
                if(Program.QQRuntimeInspect)await InspectQQRuntime();
                if(Program.QQDirectoryInspect)await InspectQQDirectory();
            }
        }
        catch (Exception error)
        {
            Result = 1; Log("startup_failed", new { type=error.GetType().Name, error.Message });
            fallback.Text = "窗口组件未能启动。请查看本机诊断记录。"; fallback.BringToFront();
            if (Program.Smoke || Program.UninstallSmoke || Program.ModelCancelSmoke || Program.AccountPickerSmoke || Program.RecoveryCheckSmoke || Program.ShutdownSmoke) { SaveResult(false); Exit(); }
        }
    }
    static bool Allowed(string uri) => Uri.TryCreate(uri, UriKind.Absolute, out var u) &&
        u.Scheme == "https" && u.Host == "qiban.internal" && u.Port == 443 && string.IsNullOrEmpty(u.UserInfo);    void Serve(CoreWebView2Environment environment, CoreWebView2WebResourceRequestedEventArgs e)
    {
        string path = new Uri(e.Request.Uri).AbsolutePath;
        if (!Allowed(e.Request.Uri) || !resources.TryGetValue(path, out var name))
        { e.Response = environment.CreateWebResourceResponse(null, 403, "Forbidden", ""); return; }
        var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name)!;
        string mime = Path.GetExtension(name) switch { ".css"=>"text/css", ".js"=>"application/javascript", ".png"=>"image/png", ".svg"=>"image/svg+xml", _=>"text/html" };
        e.Response = environment.CreateWebResourceResponse(stream, 200, "OK", "Content-Type: " + mime + "; charset=utf-8\r\n" +
            "Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'\r\nCache-Control: no-store");
    }
    async Task Receive(CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!Allowed(e.Source)) return;
        try
        {
            using var message = JsonDocument.Parse(e.WebMessageAsJson);
            if (message.RootElement.ValueKind != JsonValueKind.Object || !message.RootElement.TryGetProperty("action", out var action) || action.ValueKind != JsonValueKind.String) { Log("invalid_message_rejected"); return; }
            switch (action.GetString())
            {
                case "recover-lock": await RecoverLock(message.RootElement); break;
                case "restore-backup": await RestoreBackup(message.RootElement); break;
                case "portable-export":
                    if(preparation is not null)await UninstallPortableBackup(message.RootElement);
                    else await PortableExport(message.RootElement);
                    break;
                case "portable-preview": await PortablePreview(message.RootElement); break;
                case "portable-preserved-list": await PortablePreservedList(message.RootElement); break;
                case "portable-preserved-preview": await PortablePreservedPreview(message.RootElement); break;
                case "portable-restore": await PortableSwitch(message.RootElement); break;
                case "portable-cancel": await PortableCancel(message.RootElement); break;
                case "wechat-client-login": await OpenWechatLogin(); break;
                case "bili-browser-login": await LoginBiliBrowser(message.RootElement); break;
                case "management":
                    if(!Program.ManagedInstall || component.State!="ready" || backend is null)
                    {Post(new{type="management",request_id=message.RootElement.GetProperty("request_id").GetString(),response=new{ok=false,error="management_unavailable",message="后台尚未就绪"}});break;}
                    await control.WaitAsync();
                    try
                    {
                        Log("management_started");
                        if(exiting || component.State!="ready" || backend is null)throw new IOException("management_unavailable");
                        var request=message.RootElement;
                        string? operation=request.GetProperty("operation").GetString();
                        if(operation=="room.post_files")
                        {
                            // A path is accepted only through a File object from WebView's picker.
                            var arguments=request.GetProperty("arguments");
                            var picked=e.AdditionalObjects;
                            if(!arguments.TryGetProperty("hashes",out var hashes) || hashes.ValueKind!=JsonValueKind.Array || hashes.GetArrayLength() is <1 or >3
                                || picked is null || picked.Count!=hashes.GetArrayLength() || picked.Any(o=>o is not CoreWebView2File))
                            {
                                Post(new{type="management",request_id=request.GetProperty("request_id").GetString(),
                                    response=new{ok=false,error="invalid_image",message="请重新选择一至三张本地图片。"}});break;
                            }
                            var files=picked.Cast<CoreWebView2File>().Select((file,i)=>new{path=file.Path,sha256=hashes[i].GetString()}).ToArray();
                            request=JsonSerializer.SerializeToElement(new{action="management",operation,request_id=request.GetProperty("request_id"),
                                arguments=new{session=arguments.GetProperty("session"),text=arguments.GetProperty("text"),nonce=arguments.GetProperty("nonce"),files}});
                        }
                        var response=await StorageComponent.Call(pipeName+"-storage",backend,request);
                        if(message.RootElement.GetProperty("operation").GetString()=="model.open" && response.GetProperty("ok").GetBoolean())
                        {
                            var url=response.GetProperty("result").GetProperty("url").GetString();
                            if(!Uri.TryCreate(url,UriKind.Absolute,out var uri) || uri.Scheme!="https" || uri.Port!=443
                                || uri.UserInfo!="" || uri.Host is not ("auth.openai.com" or "chatgpt.com"))throw new IOException("model_login_origin");
                            try{Process.Start(new ProcessStartInfo(uri.AbsoluteUri){UseShellExecute=true});}
                            catch(System.ComponentModel.Win32Exception){throw new IOException("model_login_browser_unavailable");}
                            response=JsonSerializer.SerializeToElement(new{ok=true,result=new{opened=true}});
                        }
                        Log("management_completed",new{ok=response.GetProperty("ok").GetBoolean()});
                        Post(new{type="management",request_id=message.RootElement.GetProperty("request_id").GetString(),response});
                    }
                    finally{control.Release();}
                    break;
                case "status": _ = CheckService(); break;
                case "start": if (!exiting) await StartBackend(); break;
                case "stop": if (!exiting) await StopBackend(); break;
                // Uninstall preparation. The page never removes anything and never starts the
                // installer's uninstaller on its own: it asks, and this window answers from one
                // session that only a real backup success (or the person's own skip) unlocks.
                case "uninstall": BeginUninstallPreparation(); break;
                case "uninstall-status": PostUninstall(); break;
                case "uninstall-backup": await UninstallBackup(); break;
                case "uninstall-skip": SkipBackup(); break;
                case "uninstall-continue": ContinueUninstall(message.RootElement); break;
                case "tray": HideToTray(); break;
                case "exit": Exit(); break;
                case "sticker-import": await ImportSticker(message.RootElement); break;
                case "file-import": await ImportFile(message.RootElement); break;
                case "record-export": await ExportRecord(message.RootElement); break;
                case "record-import": await ImportRecord(message.RootElement); break;
                case "pick":
                    using (var dialog = new OpenFileDialog { Title="检查文件选择对话框（不读取内容）", CheckFileExists=true, Multiselect=false })
                    { bool selected=dialog.ShowDialog(this)==DialogResult.OK; Post(new {type="file",name=selected?Path.GetFileName(dialog.FileName):null});Log("file_dialog_closed",new {selected}); }
                    break;
                default: Log("unknown_action_rejected"); break;
            }
        }
        catch (Exception error) when(error is JsonException or KeyNotFoundException or InvalidOperationException)
        { Log("invalid_message_rejected"); }
        catch(Exception error) when(error is IOException or OperationCanceledException)
        {Log("management_connection_failed",new{type=error.GetType().Name});Post(new{type="management_error",message="管理连接暂时不可用，请重试。"});}
    }
    async Task OpenWechatLogin()
    {
        await control.WaitAsync(lifetime.Token);
        try
        {
            if(exiting || !Program.ManagedInstall || backend is null || component.State!="ready")throw new IOException("wechat_login_unavailable");
            var status=await RecordCall(backend,Guid.NewGuid().ToString("N"),"wechat.profiles",new{});
            if(!status.GetProperty("ok").GetBoolean())
            {Post(new{type="wechat-login",message="请先解锁本机管理，再打开微信登录。"});return;}
            var candidates=new List<string>();
            foreach(var name in new[]{"Weixin","WeChat"})
                foreach(var process in Process.GetProcessesByName(name))
                    using(process)
                    {
                        try{if(process.MainModule?.FileName is string path)candidates.Add(path);}
                        catch(Exception error) when(error is System.ComponentModel.Win32Exception or InvalidOperationException){}
                    }
            foreach(var name in new[]{@"Software\Tencent\Weixin",@"Software\Tencent\WeChat"})
            {
                using var key=Microsoft.Win32.Registry.CurrentUser.OpenSubKey(name);
                if(key?.GetValue("InstallPath") is string folder)
                    foreach(var file in new[]{"Weixin.exe","WeChat.exe"})candidates.Add(Path.Combine(folder,file));
            }
            var executable=candidates.Distinct(StringComparer.OrdinalIgnoreCase).FirstOrDefault(path=>
                File.Exists(path) && (FileVersionInfo.GetVersionInfo(path).CompanyName is string company) &&
                (company.Contains("Tencent",StringComparison.OrdinalIgnoreCase) || company.Contains("腾讯")));
            if(executable is null)
            {Post(new{type="wechat-login",message="尚未找到电脑版微信，请先安装并打开微信，再回来检测账号。"});return;}
            Process.Start(new ProcessStartInfo(executable){UseShellExecute=true});
            Post(new{type="wechat-login",message="已打开微信，请在微信中完成手机确认或扫码，再点击检测已登录账号。"});
        }
        catch(System.ComponentModel.Win32Exception)
        {Post(new{type="wechat-login",message="微信未能打开，请手动打开微信并登录后重新检测。"});}
        finally{control.Release();}
    }
    bool biliBrowserBusy;
    async Task LoginBiliBrowser(JsonElement message)
    {
        if(message.EnumerateObject().Count()!=4 || message.GetProperty("operation").GetString()!="bili.browser")return;
        var id=message.GetProperty("request_id").GetString();
        var args=message.GetProperty("arguments");
        if(id is null || !System.Text.RegularExpressions.Regex.IsMatch(id,"\\A[a-f0-9]{32}\\z") ||
            args.EnumerateObject().Count()!=1 || args.GetProperty("session").ValueKind!=JsonValueKind.String)return;
        var session=args.GetProperty("session").GetString();
        void Reply(JsonElement response)=>Post(new{type="management",request_id=id,response});
        if(biliBrowserBusy){Reply(NativeError("bili_login_busy","请先完成或关闭当前登录窗口。"));return;}
        var expected=backend;var epoch=controlEpoch;
        async Task<JsonElement> Exchange(string operation,object arguments)
        {
            await control.WaitAsync(lifetime.Token);
            try
            {
                if(exiting || !Program.ManagedInstall || expected is null || !ReferenceEquals(expected,backend) ||
                    epoch!=controlEpoch || component.State!="ready")throw new IOException("bili_login_instance_changed");
                return await RecordCall(expected,id!,operation,arguments);
            }
            finally{control.Release();}
        }
        biliBrowserBusy=true;
        string? ticket=null;
        try
        {
            var start=await Exchange("bili.browser",new{session});
            if(!start.GetProperty("ok").GetBoolean()){Reply(start);return;}
            ticket=start.GetProperty("result").GetProperty("ticket").GetString();
            using var login=new BiliLoginWindow();
            var cookies=await login.Login(this);
            var response=cookies is null ? await Exchange("bili.cancel",new{session,ticket}) :
                await Exchange("bili.browser_complete",new{session,ticket,cookies});
            Reply(response);
        }
        catch(Exception error) when(error is IOException or OperationCanceledException)
        {Reply(NativeError("bili_login_unavailable","登录连接已结束，请重新打开登录窗口。"));}
        finally{biliBrowserBusy=false;}
    }
    async Task ImportFile(JsonElement message)
    {
        if(message.EnumerateObject().Count()!=3 || !message.TryGetProperty("request_id",out var id)
            || id.ValueKind!=JsonValueKind.String || !System.Text.RegularExpressions.Regex.IsMatch(id.GetString()!,"\\A[a-f0-9]{32}\\z")
            || !message.TryGetProperty("tags",out var tags) || tags.ValueKind!=JsonValueKind.Array)
        {Log("invalid_message_rejected");return;}
        await control.WaitAsync(lifetime.Token);
        try
        {
            if(exiting || !Program.ManagedInstall || backend is null || component.State!="ready")throw new IOException("management_unavailable");
            var status=await StorageComponent.Call(pipeName+"-storage",backend,JsonSerializer.SerializeToElement(new{
                action="management",operation="files.list",arguments=new{after="",limit=1},request_id=Guid.NewGuid().ToString("N")}));
            if(!status.GetProperty("ok").GetBoolean()){Post(new{type="management",request_id=id.GetString(),response=status});return;}
            using var dialog=new OpenFileDialog{Title="导入文件到栖伴（不会立即发送）",CheckFileExists=true,Multiselect=false,Filter="所有文件|*.*"};
            if(dialog.ShowDialog(this)!=DialogResult.OK)
            {Post(new{type="management",request_id=id.GetString(),response=new{ok=false,error="cancelled",message="已取消导入，文件库未改变。"}});return;}
            var response=await StorageComponent.Call(pipeName+"-storage",backend,JsonSerializer.SerializeToElement(new{
                action="management",operation="files.import_file",arguments=new{path=dialog.FileName,tags},request_id=id.GetString()}));
            Post(new{type="management",request_id=id.GetString(),response});
        }
        finally{control.Release();}
    }
    async Task ImportSticker(JsonElement message)
    {
        if(message.EnumerateObject().Count()!=4 || !message.TryGetProperty("request_id",out var id)
            || id.ValueKind!=JsonValueKind.String || !System.Text.RegularExpressions.Regex.IsMatch(id.GetString()!,"\\A[a-f0-9]{32}\\z")
            || !message.TryGetProperty("name",out var name) || name.ValueKind!=JsonValueKind.String
            || !message.TryGetProperty("tags",out var tags) || tags.ValueKind!=JsonValueKind.Array)
        {Log("invalid_message_rejected");return;}
        await control.WaitAsync(lifetime.Token);
        try
        {
            if(exiting || !Program.ManagedInstall || backend is null || component.State!="ready")throw new IOException("management_unavailable");
            var status=await StorageComponent.Call(pipeName+"-storage",backend,JsonSerializer.SerializeToElement(new{
                action="management",operation="stickers.list",arguments=new{after="",limit=1},request_id=Guid.NewGuid().ToString("N")}));
            if(!status.GetProperty("ok").GetBoolean()){Post(new{type="management",request_id=id.GetString(),response=status});return;}
            using var dialog=new OpenFileDialog{Title="导入表情到栖伴（保存原图，不会立即发送）",CheckFileExists=true,Multiselect=false,Filter="图片|*.png;*.jpg;*.jpeg;*.webp;*.gif"};
            if(dialog.ShowDialog(this)!=DialogResult.OK)
            {Post(new{type="management",request_id=id.GetString(),response=new{ok=false,error="cancelled",message="已取消导入，表情库未改变。"}});return;}
            string label=string.IsNullOrWhiteSpace(name.GetString())?Path.GetFileNameWithoutExtension(dialog.FileName):name.GetString()!;
            var response=await StorageComponent.Call(pipeName+"-storage",backend,JsonSerializer.SerializeToElement(new{
                action="management",operation="stickers.import_file",arguments=new{path=dialog.FileName,name=label,tags},request_id=id.GetString()}));
            Post(new{type="management",request_id=id.GetString(),response});
        }
        finally{control.Release();}
    }
    static readonly string[] RecordKinds = { "memories","knowledge","diaries" };
    static bool ValidNativeText(string? value,int max)=>!string.IsNullOrEmpty(value) && value.Length<=max
        && value.All(character=>!char.IsControl(character));
    static bool TryRecordRequest(JsonElement message,out string request,out string kind,out string identifier,out string expected)
    {
        request=kind=identifier=expected="";
        if(message.ValueKind!=JsonValueKind.Object || message.EnumerateObject().Count()!=5)return false;
        if(!message.TryGetProperty("request_id",out var id) || id.ValueKind!=JsonValueKind.String
            || !System.Text.RegularExpressions.Regex.IsMatch(id.GetString()!,"\\A[a-f0-9]{32}\\z"))return false;
        if(!message.TryGetProperty("kind",out var type) || type.ValueKind!=JsonValueKind.String)return false;
        var typeName=type.GetString();
        if(typeName is null || Array.IndexOf(RecordKinds,typeName)<0)return false;
        if(!message.TryGetProperty("identifier",out var name) || name.ValueKind!=JsonValueKind.String
            || !ValidNativeText(name.GetString(),240))return false;
        if(!message.TryGetProperty("expected_version",out var version) || version.ValueKind!=JsonValueKind.String
            || !ValidNativeText(version.GetString(),200))return false;
        request=id.GetString()!;kind=typeName;identifier=name.GetString()!;expected=version.GetString()!;
        return true;
    }
    static JsonElement NativeError(string code,string message)=>JsonSerializer.SerializeToElement(new{ok=false,error=code,message});
    static string NativeFileName(string? name,string fallback)
    {
        var value=string.IsNullOrWhiteSpace(name)?fallback:name!;
        foreach(var invalid in Path.GetInvalidFileNameChars())value=value.Replace(invalid,'_');
        return value.EndsWith(".yaml",StringComparison.OrdinalIgnoreCase) || value.EndsWith(".yml",StringComparison.OrdinalIgnoreCase)
            ?value:value+".yaml";
    }
    // Requests stay one JSON line. Non-ASCII BMP text crosses it as literal Unicode;
    // escapes (including surrogate pairs such as emoji) are bounded by the 262144-char
    // command read in StorageComponent, which is what keeps a 24000-byte file deliverable.
    static readonly JsonSerializerOptions PipeJson = new()
        {Encoder=System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping};
    static string RecordLine<T>(string request,string operation,T arguments)=>
        JsonSerializer.Serialize(new{action="management",operation,arguments,request_id=request},PipeJson);
    async Task<JsonElement> RecordCall<T>(Process server,string request,string operation,T arguments)
    {
        using var document=JsonDocument.Parse(RecordLine(request,operation,arguments));
        return await StorageComponent.Call(pipeName+"-storage",server,document.RootElement.Clone());
    }
    async Task ExportRecord(JsonElement message)
    {
        if(!TryRecordRequest(message,out var id,out var kind,out var identifier,out var expected)){Log("invalid_message_rejected");return;}
        void Reply(JsonElement response)=>Post(new{type="management",request_id=id,response});
        await control.WaitAsync(lifetime.Token);
        try
        {
            var server=backend;
            if(exiting || !Program.ManagedInstall || server is null || component.State!="ready")
            {Reply(NativeError("management_unavailable","后台尚未就绪"));return;}
            // Authorize before any window appears, then revalidate after the picker.
            var status=await RecordCall(server,Guid.NewGuid().ToString("N"),"records.list",new{kind,after="",limit=1});
            if(!status.GetProperty("ok").GetBoolean()){Reply(status);return;}
            using var dialog=new SaveFileDialog{Title="导出高级资料 YAML（明文文件，含真实来源清单）",
                Filter="YAML 文件|*.yaml;*.yml|所有文件|*.*",DefaultExt="yaml",AddExtension=true,OverwritePrompt=true,
                FileName=NativeFileName(null,identifier)};
            if(dialog.ShowDialog(this)!=DialogResult.OK){Reply(NativeError("cancelled","已取消导出，资料未改变。"));return;}
            var current=await RecordCall(server,Guid.NewGuid().ToString("N"),"records.read",new{kind,identifier});
            if(!current.GetProperty("ok").GetBoolean()){Reply(current);return;}
            if(current.GetProperty("result").GetProperty("version").GetString()!=expected)
            {Reply(NativeError("edit_conflict","资料在导出前已变化，请重新读取后再导出。"));return;}
            var exported=await RecordCall(server,id,"records.advanced_export",new{kind,identifier});
            if(!exported.GetProperty("ok").GetBoolean()){Reply(exported);return;}
            var result=exported.GetProperty("result");
            if(result.GetProperty("version").GetString()!=expected)
            {Reply(NativeError("edit_conflict","资料在导出前已变化，请重新读取后再导出。"));return;}
            var content=new UTF8Encoding(false).GetBytes(result.GetProperty("text").GetString()??"");
            try
            {
                File.WriteAllBytes(dialog.FileName,content);
            }
            catch(Exception error) when(error is IOException or UnauthorizedAccessException or System.Security.SecurityException)
            {Log("record_export_write_failed",new{type=error.GetType().Name});Reply(NativeError("export_failed","文件未能写入，请选择其他位置后重试。"));return;}
            int bytes=content.Length;
            Log("record_export_completed",new{bytes});
            Reply(JsonSerializer.SerializeToElement(new{ok=true,result=new{saved=true,filename=Path.GetFileName(dialog.FileName),version=expected,bytes}}));
        }
        catch(IOException){Reply(NativeError("management_unavailable","管理连接暂时不可用，请重试。"));}
        finally{control.Release();}
    }
    async Task ImportRecord(JsonElement message)
    {
        if(!TryRecordRequest(message,out var id,out var kind,out var identifier,out var expected)){Log("invalid_message_rejected");return;}
        void Reply(JsonElement response)=>Post(new{type="management",request_id=id,response});
        await control.WaitAsync(lifetime.Token);
        try
        {
            var server=backend;
            if(exiting || !Program.ManagedInstall || server is null || component.State!="ready")
            {Reply(NativeError("management_unavailable","后台尚未就绪"));return;}
            var status=await RecordCall(server,Guid.NewGuid().ToString("N"),"records.list",new{kind,after="",limit=1});
            if(!status.GetProperty("ok").GetBoolean()){Reply(status);return;}
            using var dialog=new OpenFileDialog{Title="选择高级资料 YAML 文件（先预览，确认前不会写入）",
                CheckFileExists=true,Multiselect=false,Filter="YAML 文件|*.yaml;*.yml|所有文件|*.*"};
            if(dialog.ShowDialog(this)!=DialogResult.OK){Reply(NativeError("cancelled","已取消导入，资料未改变。"));return;}
            // The picker can stay open indefinitely; revalidate before reading the file.
            var current=await RecordCall(server,Guid.NewGuid().ToString("N"),"records.read",new{kind,identifier});
            if(!current.GetProperty("ok").GetBoolean()){Reply(current);return;}
            if(current.GetProperty("result").GetProperty("version").GetString()!=expected)
            {Reply(NativeError("edit_conflict","资料在预览前已变化，请重新读取后再选择文件。"));return;}
            string text="";
            // Read at most one byte past the documented limit, so a file that grows after
            // the picker cannot bypass the 24000-byte bound.
            var raw=new byte[24001];
            int length=0;
            try
            {
                using var stream=new FileStream(dialog.FileName,FileMode.Open,FileAccess.Read,FileShare.Read);
                while(length<raw.Length)
                {
                    var count=await stream.ReadAsync(raw.AsMemory(length,raw.Length-length));
                    if(count<=0)break;
                    length+=count;
                }
            }
            catch(Exception error) when(error is IOException or UnauthorizedAccessException or System.Security.SecurityException)
            {Log("record_import_read_failed",new{type=error.GetType().Name});Reply(NativeError("import_failed","文件未能读取，请检查文件后重试。"));return;}
            if(length==0){Reply(NativeError("import_empty","所选文件为空，请选择由本应用导出的高级资料文件。"));return;}
            if(length>24000){Reply(NativeError("import_too_large","完整资料文件超过 24 KB 导入上限，请先缩小文件。"));return;}
            try{text=new UTF8Encoding(false,true).GetString(raw,0,length).TrimStart('\uFEFF');}
            catch(DecoderFallbackException){Reply(NativeError("import_encoding","请选择 UTF-8 编码的 YAML 文件。"));return;}
            var previewLine=RecordLine(id,"records.advanced_preview",new{kind,identifier,text,expected_version=expected});
            // Matches the transport bound in StorageComponent; the 24000-byte file limit
            // above is what actually constrains the recorded content.
            if(previewLine.Length>262144)
            {Reply(NativeError("import_too_large","所选文件超出本次导入的读取上限，请先缩小文件。"));return;}
            using var previewDocument=JsonDocument.Parse(previewLine);
            var preview=await StorageComponent.Call(pipeName+"-storage",server,previewDocument.RootElement.Clone());
            if(!preview.GetProperty("ok").GetBoolean()){Reply(preview);return;}
            Log("record_import_preview_ready");Reply(preview);
        }
        catch(IOException){Reply(NativeError("management_unavailable","管理连接暂时不可用，请重试。"));}
        finally{control.Release();}
    }
    void Post(object data) { if (loaded && Allowed(view.Source.AbsoluteUri)) view.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(data)); }
    async Task RecoverLock(JsonElement message)
    {
        if(message.EnumerateObject().Count()!=3 || !message.TryGetProperty("request_id",out var id)
            || id.ValueKind!=JsonValueKind.String || !System.Text.RegularExpressions.Regex.IsMatch(id.GetString()!,"\\A[a-f0-9]{32}\\z")
            || !message.TryGetProperty("password",out var password) || password.ValueKind!=JsonValueKind.String)
        {Log("invalid_message_rejected");return;}
        void Reply(JsonElement response)=>Post(new{type="management",request_id=id.GetString(),response});
        if(recovering || !Program.ManagedInstall || backend is null || component.State!="ready")
        {Reply(JsonSerializer.SerializeToElement(new{ok=false,error="recovery_unavailable",message="后台尚未就绪或正在验证，请稍后再试。"}));return;}
        recovering=true;
        var expected=backend;long epoch=controlEpoch;
        async Task<JsonElement> Exchange(string operation,object arguments)
        {
            await control.WaitAsync(lifetime.Token);
            try
            {
                if(exiting || epoch!=controlEpoch || !ReferenceEquals(expected,backend) || component.State!="ready")throw new IOException("recovery_instance_changed");
                return await StorageComponent.Call(pipeName+"-storage",expected,
                    JsonSerializer.SerializeToElement(new{action="desktop-recovery",operation,arguments}));
            }
            finally{control.Release();}
        }
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            timeout.CancelAfter(TimeSpan.FromSeconds(180));
            var flow=new LockRecovery(Exchange,token=>WindowsVerification.Verify(Handle,token));
            var response=await flow.Run(password.GetString()!,timeout.Token);
            Log("windows_recovery_completed",new{ok=response.GetProperty("ok").GetBoolean()});
            Reply(response);
        }
        finally{recovering=false;}
    }
    async Task RestoreBackup(JsonElement message)
    {
        if(message.EnumerateObject().Count()!=3 || !message.TryGetProperty("request_id",out var id)
            || id.ValueKind!=JsonValueKind.String || !System.Text.RegularExpressions.Regex.IsMatch(id.GetString()!,"\\A[a-f0-9]{32}\\z")
            || !message.TryGetProperty("token",out var preview) || preview.ValueKind!=JsonValueKind.String)
        {Log("invalid_message_rejected");return;}
        void Reply(JsonElement response)=>Post(new{type="management",request_id=id.GetString(),response});
        if(recovering || !Program.ManagedInstall || backend is null || component.State!="ready")
        {Reply(JsonSerializer.SerializeToElement(new{ok=false,error="restore_unavailable",message="后台尚未就绪或正在验证，请稍后再试。"}));return;}
        recovering=true;
        var expected=backend;long epoch=controlEpoch;
        async Task<JsonElement> Exchange(string operation,object arguments)
        {
            await control.WaitAsync(lifetime.Token);
            try
            {
                if(exiting || epoch!=controlEpoch || !ReferenceEquals(expected,backend) || component.State!="ready")throw new IOException("restore_instance_changed");
                return await StorageComponent.Call(pipeName+"-storage",expected,
                    JsonSerializer.SerializeToElement(new{action="desktop-restore",operation,arguments}));
            }
            finally{control.Release();}
        }
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            timeout.CancelAfter(TimeSpan.FromSeconds(180));
            var flow=new BackupRestore(Exchange,token=>WindowsVerification.VerifyRestore(Handle,token));
            var response=await flow.Run(preview.GetString()!,timeout.Token);
            Log("windows_restore_completed",new{ok=response.GetProperty("ok").GetBoolean()});
            Reply(response);
        }
        finally{recovering=false;}
    }
    // Portable cross-machine transfer (D54/D55). Paths come only from the pickers below:
    // a page request carries a recovery password or a preview token and nothing else. The
    // password is forwarded to the private storage pipe and is never logged, echoed back,
    // stored or repeated in an error message.
    void PortableReply(string request,JsonElement response)=>Post(new{type="management",request_id=request,response});
    async Task<JsonElement> PortableSend(Process expected,long epoch,JsonElement request)
    {
        await control.WaitAsync(lifetime.Token);
        try
        {
            // The epoch is the one captured before any picker or Windows prompt: a lifecycle
            // change during a dialog invalidates the whole transfer, not only its next call.
            if(exiting || epoch!=controlEpoch || !ReferenceEquals(expected,backend) || component.State!="ready")
                throw new IOException("portable_instance_changed");
            return await StorageComponent.Call(pipeName+"-storage",expected,request);
        }
        finally{control.Release();}
    }
    // Authorize before any window appears, so a picker does not open for a backend that
    // cannot answer or for a locked session; every real operation checks again.
    static JsonElement PortableSessionStatus()=>JsonSerializer.SerializeToElement(new{
        action="management",operation="session.status",arguments=new{},request_id=Guid.NewGuid().ToString("N")});
    static bool PortableUsable(Process? server)=>Program.ManagedInstall && server is {HasExited:false};
    // Returns the reply it also posted, so an uninstall preparation run can record a real exported
    // pair as this run's backup without reading anything back from the page.
    async Task<JsonElement> PortableExport(JsonElement message)
    {
        if(!DesktopPortable.TryPassword("portable-export",message,out var id,out var password)){Log("invalid_message_rejected");return default;}
        if(portableBusy || recovering){var busy=NativeError("portable_busy","上一项便携备份操作尚未完成，请稍后再试。");PortableReply(id,busy);return busy;}
        var expected=backend;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {var missing=NativeError("management_unavailable","后台尚未就绪，暂时无法导出便携备份。");PortableReply(id,missing);return missing;}
        long epoch=controlEpoch;
        DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
        portableBusy=true;
        try
        {
            var status=await PortableSend(expected!,epoch,PortableSessionStatus());
            if(!DesktopPortable.Ok(status)){PortableReply(id,status);return status;}
            if(!DesktopPortable.Unlocked(status))
            {var locked=NativeError("desktop_locked","栖伴已锁定，请先解锁再导出便携备份。");PortableReply(id,locked);return locked;}
            using var dialog=new SaveFileDialog{Title="导出便携备份（.ccbackup 与同名 .cckey 必须成对保管）",
                Filter="栖伴便携备份|*.ccbackup|所有文件|*.*",DefaultExt="ccbackup",AddExtension=true,OverwritePrompt=true,
                FileName="qiban-便携备份-"+DateTime.Now.ToString("yyyyMMdd-HHmm")};
            if(dialog.ShowDialog(this)!=DialogResult.OK)
            {var cancelled=NativeError("cancelled","已取消导出，当前资料未改变。");PortableReply(id,cancelled);return cancelled;}
            var response=await DesktopPortable.Export(exchange,dialog.FileName,password,dialog.FileName);
            Log("portable_export_completed",new{ok=DesktopPortable.Ok(response)});
            PortableReply(id,response);
            return response;
        }
        catch(IOException){var failed=NativeError("management_unavailable","管理连接暂时不可用，请重试。");PortableReply(id,failed);return failed;}
        finally{portableBusy=false;}
    }
    async Task PortablePreview(JsonElement message)
    {
        if(!DesktopPortable.TryPassword("portable-preview",message,out var id,out var password)){Log("invalid_message_rejected");return;}
        if(portableBusy || recovering){PortableReply(id,NativeError("portable_busy","上一项便携备份操作尚未完成，请稍后再试。"));return;}
        var expected=backend;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {PortableReply(id,NativeError("management_unavailable","后台尚未就绪，暂时无法预览便携备份。"));return;}
        long epoch=controlEpoch;
        DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
        portableBusy=true;
        try
        {
            var status=await PortableSend(expected!,epoch,PortableSessionStatus());
            if(!DesktopPortable.Ok(status)){PortableReply(id,status);return;}
            if(!DesktopPortable.Unlocked(status))
            {PortableReply(id,NativeError("desktop_locked","栖伴已锁定，请先解锁再预览便携备份。"));return;}
            using var backupDialog=new OpenFileDialog{Title="选择便携备份文件（.ccbackup）",CheckFileExists=true,
                Multiselect=false,Filter="栖伴便携备份|*.ccbackup|所有文件|*.*"};
            if(backupDialog.ShowDialog(this)!=DialogResult.OK)
            {PortableReply(id,NativeError("cancelled","已取消导入预览，当前资料未改变。"));return;}
            using var keyDialog=new OpenFileDialog{Title="选择同名的独立恢复密钥文件（.cckey）",CheckFileExists=true,
                Multiselect=false,Filter="栖伴恢复密钥|*.cckey|所有文件|*.*",InitialDirectory=Path.GetDirectoryName(backupDialog.FileName)};
            if(keyDialog.ShowDialog(this)!=DialogResult.OK)
            {PortableReply(id,NativeError("cancelled","已取消导入预览，当前资料未改变。"));return;}
            // Both paths leave this window exactly once and never travel back to the page.
            var response=await DesktopPortable.Preview(exchange,backupDialog.FileName,keyDialog.FileName,password);
            if(DesktopPortable.Ok(response))portablePending=true;
            Log("portable_preview_completed",new{ok=DesktopPortable.Ok(response)});
            PortableReply(id,response);
        }
        catch(IOException){PortableReply(id,NativeError("management_unavailable","管理连接暂时不可用，请重试。"));}
        finally{portableBusy=false;}
    }
    // This machine's own retained copies. No password and no picker: the window forwards
    // only the backend's continuation cursor or the opaque handle it issued for a copy this
    // installation enumerated, and the backend derives every address from its own root.
    async Task PortablePreservedList(JsonElement message)
    {
        if(!DesktopPreserved.TryList(message,out var id,out var after)){Log("invalid_message_rejected");return;}
        if(portableBusy || recovering){PortableReply(id,NativeError("portable_busy","上一项便携备份操作尚未完成，请稍后再试。"));return;}
        var expected=backend;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {PortableReply(id,NativeError("management_unavailable","后台尚未就绪，暂时无法读取本机保全资料。"));return;}
        long epoch=controlEpoch;
        DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
        portableBusy=true;
        try
        {
            var status=await PortableSend(expected!,epoch,PortableSessionStatus());
            if(!DesktopPortable.Ok(status)){PortableReply(id,status);return;}
            if(!DesktopPortable.Unlocked(status))
            {PortableReply(id,NativeError("desktop_locked","栖伴已锁定，请先解锁再读取本机保全资料。"));return;}
            var response=await DesktopPreserved.List(exchange,after);
            Log("preserved_list_completed",new{ok=DesktopPortable.Ok(response)});
            PortableReply(id,response);
        }
        catch(IOException){PortableReply(id,NativeError("management_unavailable","管理连接暂时不可用，请重试。"));}
        finally{portableBusy=false;}
    }
    async Task PortablePreservedPreview(JsonElement message)
    {
        if(!DesktopPreserved.TryPreview(message,out var id,out var handle)){Log("invalid_message_rejected");return;}
        if(portableBusy || recovering){PortableReply(id,NativeError("portable_busy","上一项便携备份操作尚未完成，请稍后再试。"));return;}
        var expected=backend;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {PortableReply(id,NativeError("management_unavailable","后台尚未就绪，暂时无法预览本机保全资料。"));return;}
        long epoch=controlEpoch;
        DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
        portableBusy=true;
        try
        {
            var status=await PortableSend(expected!,epoch,PortableSessionStatus());
            if(!DesktopPortable.Ok(status)){PortableReply(id,status);return;}
            if(!DesktopPortable.Unlocked(status))
            {PortableReply(id,NativeError("desktop_locked","栖伴已锁定，请先解锁再预览本机保全资料。"));return;}
            var response=await DesktopPreserved.Preview(exchange,handle);
            // A later Windows confirmation replaces the current data; closing the window
            // must therefore cancel instead of leaving the preview armed.
            if(DesktopPortable.Ok(response))portablePending=true;
            Log("preserved_preview_completed",new{ok=DesktopPortable.Ok(response)});
            PortableReply(id,response);
        }
        catch(IOException){PortableReply(id,NativeError("management_unavailable","管理连接暂时不可用，请重试。"));}
        finally{portableBusy=false;}
    }
    async Task PortableSwitch(JsonElement message)
    {
        if(!DesktopPortable.TryToken(message,out var id,out var token)){Log("invalid_message_rejected");return;}
        if(portableBusy || recovering){PortableReply(id,NativeError("portable_busy","本次导入尚未结束，请稍后再确认。"));return;}
        var expected=backend;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {PortableReply(id,NativeError("management_unavailable","后台尚未就绪，暂时无法恢复。"));return;}
        long epoch=controlEpoch;
        DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
        portableBusy=true;portableActive=true;
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            // begin, the Windows verification and complete all spend from this budget.
            timeout.CancelAfter(DesktopPortable.RestoreTimeout);
            var flow=new PortableRestore(exchange,token=>WindowsVerification.VerifyRestore(Handle,token));
            JsonElement response;
            try{response=await flow.Run(token,timeout.Token);}
            catch(Exception error) when(error is IOException or OperationCanceledException)
            {
                var lost=DesktopPortable.Classify(default,flow.CompleteSent);
                Log("portable_switch_reply_missing",new{type=error.GetType().Name,outcome=lost.ToString()});
                await PortableSwitchResult(id,lost,default);
                return;
            }
            var outcome=DesktopPortable.Classify(response,flow.CompleteSent);
            Log("portable_switch_replied",new{outcome=outcome.ToString(),error=DesktopPortable.Read(response,"error")});
            await PortableSwitchResult(id,outcome,response);
        }
        finally{portableBusy=false;portableActive=false;}
    }
    // One switch outcome, one host action. Completed and Uncertain both mean the old
    // backend may no longer write: it is stopped, and nothing offers a resubmission.
    // A refusal happened before the switch, so the backend keeps running and the user is
    // asked to preview again.
    async Task PortableSwitchResult(string id,PortableSwitchOutcome outcome,JsonElement response)
    {
        portablePending=false;
        if(outcome==PortableSwitchOutcome.NotStarted)
        {PortableReply(id,NativeError("management_unavailable","管理连接暂时不可用，请重试。"));return;}
        if(outcome==PortableSwitchOutcome.Refused)
        {PortableReply(id,DesktopPortable.Refusal(response,"后台未完成切换，当前资料未改变；请重新预览后再试。"));return;}
        // A switched or possibly switched backend must not keep writing the previous
        // database. The window stays stopped on purpose: the user restarts it with the
        // existing start button, which reloads the committed generation.
        await StopBackend();
        string guidance=outcome==PortableSwitchOutcome.Completed
            ?"便携恢复已切换完成，已停止旧后台。请点击『启动应用后台』重新启动后台以载入恢复的资料；角色保持停止，平台需重新检查授权。"
            :"恢复结果未知：切换步骤可能已提交。已停止旧后台且不会重复提交，请点击『启动应用后台』重新启动后台后核对资料。";
        Log("portable_switch_completed",new{outcome=outcome.ToString(),state=component.State});
        PortableReply(id,DesktopPortable.SwitchReply(response,true,guidance));
    }
    async Task PortableCancel(JsonElement message)
    {
        if(!DesktopPortable.TryCancel(message,out var id)){Log("invalid_message_rejected");return;}
        if(portableBusy || portableActive){PortableReply(id,NativeError("portable_busy","本次导入仍在进行，请等待当前步骤完成。"));return;}
        var expected=backend;
        portablePending=false;
        if(exiting || !PortableUsable(expected) || component.State!="ready")
        {PortableReply(id,DesktopPortable.Failure("cancelled","已放弃本次导入，未执行切换。"));return;}
        long epoch=controlEpoch;
        try
        {
            DesktopPortable.Exchange exchange=(operation,arguments)=>PortableSend(expected!,epoch,DesktopPortable.Envelope(operation,arguments));
            var response=await DesktopPortable.Cancel(exchange);
            Log("portable_cancel_completed",new{ok=DesktopPortable.Ok(response)});
            PortableReply(id,response);
        }
        catch(IOException){PortableReply(id,DesktopPortable.Failure("cancelled","已放弃本次导入，未执行切换；后台未收到取消通知。"));}
    }
    // Closing the window cancels an outstanding import and never sends complete. The
    // wait is bounded so a slow backend cannot hold the application open.
    async Task PortableCancelOnExit()
    {
        var expected=backend;
        if(expected is null || component.State!="ready")return;
        try
        {
            using var bounded=new CancellationTokenSource(TimeSpan.FromSeconds(8));
            if(!await control.WaitAsync(TimeSpan.FromSeconds(3),bounded.Token))return;
            try
            {
                if(component.State!="ready" || !ReferenceEquals(expected,backend))return;
                await StorageComponent.Call(pipeName+"-storage",expected,
                    DesktopPortable.Envelope(DesktopPortable.CancelOperation,new{})).WaitAsync(bounded.Token);
                Log("portable_cancel_on_exit");
            }
            finally{control.Release();}
        }
        catch(Exception error) when(error is IOException or OperationCanceledException or TimeoutException)
        {Log("portable_cancel_on_exit_failed",new{type=error.GetType().Name});}
    }
    async Task StartBackend()
    {
        long requested=controlEpoch;
        await control.WaitAsync();
        try
        {
            if(exiting || requested!=controlEpoch)return;
            await component.StartAsync();
            if(journalStarted && component.State=="ready")
            {
                journal.Verified();
                if(journal.NeedsRecovery)
                {
                    recoveryStatus.Text="上次未正常退出；数据库恢复、资料解密与关键资料检查已通过。未确认发送不会自动补发。";
                    recoveryNotice.Succeeded(clock.ElapsedMilliseconds);
                    Log("recovery_check_completed",new{scope="database_start_and_critical_records"});
                }
            }
            else if(journal.NeedsRecovery)
            {
                recoveryNotice.Failed();
                recoveryStatus.Text="恢复检查未通过，后台未启动。请查看诊断；现有资料已保留。";
                Log("recovery_check_failed");
            }
            if (backend is {HasExited:false}) Log("test_service_started", new {pid=backend.Id});
        }
        finally{control.Release();}
    }
    async Task StopBackend()
    {
        controlEpoch++;
        component.CancelPendingStart();
        await control.WaitAsync();
        try{await component.StopAsync();}
        finally{control.Release();}
    }
    void PublishService()
    {
        if (!exiting) Post(new {type="service",ready=component.State=="ready",state=component.State,detail=component.Detail,component=component.Id,members=component.Snapshot,management=Program.ManagedInstall,windowsUser=System.Security.Principal.WindowsIdentity.GetCurrent().Name});
    }
    async Task CheckService()
    {
        // A portable transfer owns the pipe until it finishes. The heartbeat must not
        // take the control slot during the switch, or a slow backend would be reported
        // as lost and the page would be told to retry a replacement it must not repeat.
        if (checking || exiting || portableActive || !control.Wait(0)) return;
        checking=true;
        try { await component.CheckAsync(); PublishService(); }
        finally { checking=false; control.Release(); }
    }
    async Task Until(Func<Task<bool>> condition,int timeoutMilliseconds=10000)
    {
        var deadline=Stopwatch.StartNew();
        while(deadline.ElapsedMilliseconds<timeoutMilliseconds){if(await condition())return;await Task.Delay(50);}
        throw new TimeoutException("Desktop smoke assertion timed out");
    }
    async Task<bool> JsBool(string expression) => await view.CoreWebView2.ExecuteScriptAsync(expression)=="true";
    Task UntilBackendReady()=>Until(async()=>
    {
        if(component.State=="failed")throw new IOException(component.Detail??"backend_start_failed");
        return await JsBool("document.body.dataset.service==='ready'");
    },Program.ManagedInstall?240000:10000); // 180+30+30 second component budgets; the upgrade confirmation answers before the back-end starts and normally reads no payload.
    async Task BiliQrHandoff()
    {
        await Until(()=>JsBool("!document.querySelector('#bili-panel').hidden && !document.querySelector('#bili-login').disabled"),60000);
        var core=view.CoreWebView2;
        await core.ExecuteScriptAsync("document.querySelector('#bili-panel').scrollIntoView();document.querySelector('#bili-login').click()");
        await Until(()=>JsBool("!document.querySelector('#bili-qr').hidden && document.querySelector('#bili-qr').naturalWidth>0"),30000);
        var src=JsonSerializer.Deserialize<string>(await core.ExecuteScriptAsync("document.querySelector('#bili-qr').src"));
        const string prefix="data:image/png;base64,";
        if(src is null || !src.StartsWith(prefix,StringComparison.Ordinal))throw new IOException("bili_qr_unavailable");
        var path=Path.Combine(Program.Data,"bili-login-"+Environment.ProcessId+".png");
        await File.WriteAllBytesAsync(path,Convert.FromBase64String(src[prefix.Length..]));
        Log("bili_qr_handoff_ready");
        // Keep the login UI polling even after the remote chat returns to the owner.
        await Until(()=>JsBool("document.querySelector('#bili-qr').hidden"),210000);
        File.Delete(path);
        Log("bili_qr_handoff_ended");
    }
    async Task InspectQQ()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#qq-refresh').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#qq-refresh').click();document.querySelector('#qq-panel').scrollIntoView({behavior:'instant'})");
            var wait=Stopwatch.StartNew();
            while(wait.Elapsed<TimeSpan.FromSeconds(60))
            {
                if(await JsBool("document.querySelector('#qq-status').textContent==='尚无可用 QQ 账号，请扫码登录。' && !document.querySelector('#qq-login').disabled")){passed=true;break;}
                await Task.Delay(250);
            }
            if(!passed)throw new IOException("qq_fresh_account_check_failed");
            if(!await JsBool("document.querySelector('#qq-qr-area').hidden && document.querySelector('#qq-selection-area').hidden && document.querySelector('#qq-select').disabled && document.querySelector('#qq-current').textContent===''") )
                throw new IOException("qq_fresh_selection_not_empty");
            using var screenshot=File.Create(Path.Combine(Program.Data,"qq-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"qq-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task InspectQQTargets()
    {
        bool passed=false;
        try
        {
            using var input=JsonDocument.Parse(File.ReadAllText(Program.QQTargetsInspect!));
            var peers=string.Join("\n",input.RootElement.GetProperty("private_targets").EnumerateArray().Select(x=>x.GetString()));
            var groups=string.Join("\n",input.RootElement.GetProperty("groups").EnumerateArray().Select(x=>x.GetString()));
            await Until(()=>JsBool("!document.querySelector('#qq-targets-verify').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#qq-private-targets').value="+JsonSerializer.Serialize(peers)+";document.querySelector('#qq-group-targets').value="+JsonSerializer.Serialize(groups)+";document.querySelector('#qq-targets-verify').click();document.querySelector('#qq-targets-area').scrollIntoView({behavior:'instant'})");
            var wait=Stopwatch.StartNew();
            while(wait.Elapsed<TimeSpan.FromSeconds(120))
            {
                if(await JsBool("!document.querySelector('#qq-targets-save').disabled")){passed=true;break;}
                if(await JsBool("document.querySelector('#qq-status').textContent.includes('未完成')"))break;
                await Task.Delay(250);
            }
            using var screenshot=File.Create(Path.Combine(Program.Data,"qq-targets-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"qq-targets-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State,saved=false}));}
    }
    async Task InspectQQRuntime()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("document.querySelector('#qq-targets-summary').textContent.startsWith('已保存 1 个好友、1 个群。')"));
            await Until(()=>JsBool("(()=>{const r=Array.from(document.querySelector('#runtime-platforms').children).find(r=>r.querySelector('h3')?.textContent==='QQ');return !!r && r.querySelector('p').textContent==='等待模型就绪' && r.querySelector('button').disabled && !r.querySelector('input').checked;})()"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#runtime-panel').scrollIntoView({behavior:'instant'})");
            using var screenshot=File.Create(Path.Combine(Program.Data,"qq-runtime-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
            passed=true;
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"qq-runtime-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task InspectQQDirectory()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#qq-directory-load').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#qq-directory-load').click()");
            await Until(()=>JsBool("!document.querySelector('#qq-directory-area').hidden && !document.querySelector('#qq-friend-add').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("""
                for (const id of ['qq-friend-choice', 'qq-group-choice']) {
                    const select = document.getElementById(id);
                    const choice = Array.from(select.options).find(option => option.value && !option.disabled);
                    if (choice) select.value = choice.value;
                }
                document.querySelector('#qq-directory-area').scrollIntoView({behavior:'instant'});
                """);
            passed=await JsBool("Boolean(document.querySelector('#qq-friend-choice').value && document.querySelector('#qq-group-choice').value) && document.querySelector('#qq-targets-summary').textContent.startsWith('已保存 1 个好友、1 个群。') && document.querySelector('#qq-targets-save').disabled");
            using var screenshot=File.Create(Path.Combine(Program.Data,"qq-directory-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"qq-directory-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task InspectModel()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#model-login').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-refresh').click();document.querySelector('#model-panel').scrollIntoView({behavior:'instant'})");
            var wait=Stopwatch.StartNew();
            while(wait.Elapsed<TimeSpan.FromSeconds(45))
            {
                if(await JsBool("document.querySelector('#model-status').textContent==='尚未登录，请使用 ChatGPT 账号登录。' && !document.querySelector('#model-login').disabled")){passed=true;break;}
                await Task.Delay(250);
            }
            await Task.Delay(150);
#if QIBAN_SIWC_VALIDATION
            if(!await JsBool("document.querySelector('#model-login').textContent==='Continue with ChatGPT'"))
                throw new IOException("siwc_route_not_visible");
#endif
            using var screenshot=File.Create(Path.Combine(Program.Data,"model-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"model-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task InspectAccountPicker()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#model-accounts').hidden && document.querySelector('#model-selection').options.length>0 && !document.querySelector('#model-add').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-panel').scrollIntoView({behavior:'instant'})");
            if(Program.AccountPickerSmoke)
            {
                if(await JsBool("document.querySelector('#model-selection').options.length===1"))
                {
                    await Until(()=>JsBool("!document.querySelector('#model-select').disabled && document.querySelector('#model-status').textContent.includes('交接尚未完成')"));
                    Log("check_single_account_interrupted_handoff_visible_pass");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-select').click()");
                    await Until(()=>JsBool("document.querySelector('#model-feedback').open && document.querySelector('#model-select').disabled && document.querySelector('#model-account').textContent.includes('账号 1') && !document.querySelector('#model-status').textContent.includes('交接尚未完成')"));
                    Log("check_single_account_explicit_resume_pass");
                }
                else
                {
                if(!await JsBool("document.querySelector('#model-selection').options.length===2"))throw new IOException("expected_two_synthetic_accounts");
                await view.CoreWebView2.ExecuteScriptAsync("(()=>{const s=document.querySelector('#model-selection');s.selectedIndex=1;s.dispatchEvent(new Event('change'));document.querySelector('#model-select').click();})()");
                await Until(()=>JsBool("document.querySelector('#model-feedback').open && document.querySelector('#model-account').textContent.includes('账号 2') && document.querySelector('#model-select').disabled"));
                Log("check_explicit_second_account_selection_pass");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-feedback-close').click();(()=>{const s=document.querySelector('#model-selection');s.selectedIndex=0;s.dispatchEvent(new Event('change'));document.querySelector('#model-select').click();})()");
                await Until(()=>JsBool("document.querySelector('#model-feedback').open && document.querySelector('#model-account').textContent.includes('账号 1')"));
                Log("check_explicit_return_to_first_account_pass");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-feedback-close').click()");
                await control.WaitAsync();
                try
                {
                    var result=await StorageComponent.Call(pipeName+"-storage",backend!,JsonSerializer.SerializeToElement(new{
                        action="management",operation="model.add",arguments=new{},request_id=Guid.NewGuid().ToString("N")}));
                    if(!result.GetProperty("ok").GetBoolean())throw new IOException("account_add_prepare_failed");
                }
                finally{control.Release();}
                await Until(()=>JsBool("!document.querySelector('#model-cancel').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-cancel').click()");
                await Until(()=>JsBool("document.querySelector('#model-feedback-title').textContent==='账号操作已取消' && document.querySelector('#model-feedback').open && document.querySelector('#model-selection').options.length===2 && document.querySelector('#model-selection').selectedIndex===0"));
                Log("check_cancel_add_preserves_selected_account_pass");
                }
            }
            await Task.Delay(150);
            using var screenshot=File.Create(Path.Combine(Program.Data,"account-picker-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
            passed=true;
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"account-picker-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
        if(Program.AccountPickerSmoke){SaveResult(true);Exit();}
    }
    async Task InspectModelCancel()
    {
        bool passed=false;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#model-login').disabled"));
            // Prepare the native account flow without opening a browser or accepting consent.
            await control.WaitAsync();
            try
            {
                var result=await StorageComponent.Call(pipeName+"-storage",backend!,JsonSerializer.SerializeToElement(new{
                    action="management",operation="model.login",arguments=new{},request_id=Guid.NewGuid().ToString("N")}));
                if(!result.GetProperty("ok").GetBoolean())throw new IOException("account_login_prepare_failed");
            }
            finally{control.Release();}
            var wait=Stopwatch.StartNew();
            while(wait.Elapsed<TimeSpan.FromSeconds(40))
            {
                if(await JsBool("!document.querySelector('#model-cancel').disabled && !document.querySelector('#model-open').disabled")){passed=true;break;}
                await Task.Delay(250);
            }
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-panel').scrollIntoView({behavior:'instant'})");
            await Task.Delay(150);
            using var screenshot=File.Create(Path.Combine(Program.Data,"model-cancel-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"model-cancel-visible.json"),JsonSerializer.Serialize(new{passed,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task ModelCancellationSmoke()
    {
        await InspectModelCancel();
#if QIBAN_SIWC_VALIDATION
        if(!await JsBool("document.querySelector('#model-status').textContent.includes('授权 Qiban') && document.querySelector('#model-login').textContent==='Continue with ChatGPT'"))
            throw new IOException("siwc_route_not_active");
        Log("check_siwc_independent_route_pass");
#endif
        await Until(()=>JsBool("!document.querySelector('#model-cancel').disabled && !document.querySelector('#model-open').disabled"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-cancel').click()");
        await Until(()=>JsBool("document.querySelector('#model-feedback').open && document.querySelector('#model-feedback-title').textContent==='账号操作已取消' && document.querySelector('#model-cancel').disabled && document.querySelector('#model-open').disabled"));
        Log("check_real_account_cancel_completion_modal_pass");
        using(var screenshot=File.Create(Path.Combine(Program.Data,"model-cancel-feedback.png")))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#model-feedback-close').click();document.querySelector('#model-refresh').click()");
        var wait=Stopwatch.StartNew();bool signedOut=false;
        while(wait.Elapsed<TimeSpan.FromSeconds(40))
        {
            if(await JsBool("document.querySelector('#model-status').textContent==='尚未登录，请使用 ChatGPT 账号登录。' && !document.querySelector('#model-refresh').disabled")){signedOut=true;break;}
            await Task.Delay(100);
        }
        if(!signedOut)throw new IOException("cancelled_login_not_signed_out");
        if(!await JsBool("document.querySelector('#model-notice').textContent==='已取消账号操作；请重新检查当前登录状态。' && !document.querySelector('#model-feedback').open"))throw new IOException("cancel_feedback_lost_after_refresh");
        Log("check_cancel_notice_survives_account_refresh_pass");
        await Task.Delay(2500);
        if(!await JsBool("document.querySelector('#model-notice').textContent==='已取消账号操作；请重新检查当前登录状态。'"))throw new IOException("cancel_feedback_lost_after_poll");
        Log("check_cancel_notice_survives_background_poll_pass");
        SaveResult(true);Exit();
    }
    async Task InspectRestore()
    {
        bool passed=false;int samples=0;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#backup-preview').disabled && document.querySelector('#backup-selection').options.length>0"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#backup-preview').click();document.querySelector('#backup-panel').scrollIntoView({behavior:'instant',block:'start'})");
            await Until(()=>JsBool("!document.querySelector('#backup-restore').disabled && document.querySelector('#backup-effect').textContent.includes('覆盖当前资料')"));
            var idle=Stopwatch.StartNew();
            while(idle.Elapsed<TimeSpan.FromSeconds(30))
            {
                if(component.State!="ready" || !await JsBool("!document.querySelector('#backup-restore').disabled"))throw new IOException("restore_not_ready");
                samples++;await Task.Delay(500);
            }
            using var screenshot=File.Create(Path.Combine(Program.Data,"restore-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
            passed=true;
        }
        finally{File.WriteAllText(Path.Combine(Program.Data,"restore-visible.json"),JsonSerializer.Serialize(new{passed,samples,pid=Environment.ProcessId,state=component.State}));}
    }
    async Task InspectRecovery()
    {
        bool passed=false;int samples=0;
        try
        {
            await Until(()=>JsBool("!document.querySelector('#recovery-area').hidden && !document.querySelector('#recover-lock').disabled && document.querySelector('#management-panel').dataset.locked==='true'"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#recovery-area').open=true;document.querySelector('#recovery-area').scrollIntoView({behavior:'instant',block:'start'})");
            var idle=Stopwatch.StartNew();
            while(idle.Elapsed<TimeSpan.FromSeconds(90))
            {
                if(component.State!="ready" || !await JsBool("!document.querySelector('#recovery-area').hidden && !document.querySelector('#recover-lock').disabled"))throw new IOException("recovery_not_visible_or_ready");
                samples++;await Task.Delay(500);
            }
            using var screenshot=File.Create(Path.Combine(Program.Data,"recovery-visible.png"));
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
            passed=true;Log("check_recovery_idle_visible_pass",new{samples});
        }
        finally
        {
            File.WriteAllText(Path.Combine(Program.Data,"recovery-visible.json"),JsonSerializer.Serialize(new{passed,samples,pid=Environment.ProcessId,state=component.State}));
        }
    }
    async Task Smoke()
    {
        await Until(()=>Task.FromResult(loaded));
        await Until(()=>JsBool("document.body.dataset.service==='offline'"));Log("check_offline_page_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.getElementById('start').click()");
        if(Program.FailureSmoke)
        {
            await Until(()=>JsBool("document.body.dataset.component==='failed'"));Log("check_database_failure_visible_pass");
            loaded=false;view.CoreWebView2.Navigate(Origin+"/manual");
            await Until(()=>Task.FromResult(loaded));
            await Until(()=>JsBool("document.querySelector('h1').textContent==='用户手册'"));Log("check_database_failure_page_usable_pass");
            SaveResult(true);Exit();return;
        }
        await UntilBackendReady();Log("check_ipc_health_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.getElementById('stop').click()");
        await Until(()=>JsBool("document.body.dataset.component==='stopped'"));
        if(component.Detail!="graceful_stop")throw new Exception("UI stop was not graceful");
        await view.CoreWebView2.ExecuteScriptAsync("document.getElementById('start').click()");
        await UntilBackendReady();Log("check_stop_and_restart_pass");
        if(Program.ManagedInstall)await ManagementSmoke();
        backend!.Kill(entireProcessTree:true);await backend.WaitForExitAsync();
        await Until(()=>JsBool("document.body.dataset.service==='offline'"));Log("check_backend_crash_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.getElementById('tray').click()");
        await Until(()=>Task.FromResult(!Visible));Log("check_close_to_tray_pass");
        var info=new ProcessStartInfo(Environment.ProcessPath!){UseShellExecute=false,CreateNoWindow=true};
        info.ArgumentList.Add("--data-dir");info.ArgumentList.Add(Program.Data);
        using(var second=Process.Start(info)!){await second.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));if(second.ExitCode!=0)throw new Exception("Second instance failed");}
        await Until(()=>Task.FromResult(Visible&&activations>0));Log("check_single_instance_reopen_pass");
        loaded=false;view.CoreWebView2.Navigate(Origin+"/manual");
        await Until(()=>Task.FromResult(loaded));await Until(()=>JsBool("document.querySelector('h1').textContent==='用户手册'"));Log("check_existing_page_pass");
        view.CoreWebView2.Navigate("https://example.invalid/");await Task.Delay(200);
        if(!Allowed(view.Source.AbsoluteUri))throw new Exception("External navigation escaped");Log("check_navigation_block_pass");
        loaded=false;view.CoreWebView2.Navigate(Origin+"/");await Until(()=>Task.FromResult(loaded));
        foreach(var width in new[]{620,1080}){ClientSize=new Size(width,800);await Task.Delay(150);if(!await JsBool("document.documentElement.scrollWidth<=innerWidth"))throw new Exception("Horizontal overflow");}
        Log("check_layout_widths_pass"); using(var screenshot=File.Create(Path.Combine(Program.Data,"preview.png"))) await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
        await StartBackend();
        if(component.State!="ready")throw new Exception("Service not ready for application exit check");
        SaveResult(true);Exit();
    }
    void SaveResult(bool passed) => File.WriteAllText(Path.Combine(Program.Data,"result.json"),JsonSerializer.Serialize(new{passed,process_id=Environment.ProcessId,events,elapsed_ms=clock.ElapsedMilliseconds,working_set=Environment.WorkingSet,dpi=DeviceDpi},new JsonSerializerOptions{WriteIndented=true}));

    async Task ManagementSmoke()
    {
        await Until(()=>JsBool("document.querySelector('#management-panel').dataset.locked==='false' && document.querySelector('#profile-area').dataset.loaded==='true'"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#persona-name').value='桌面资料验证';document.querySelector('#persona-description').value='独立应用内保存的测试简介';document.querySelector('#persona-speaking').value='自然交流\\n说清楚数字123';document.querySelector('#persona-interests').value='读书\\n绘画🙂';document.querySelector('#persona-name').dispatchEvent(new Event('input'));document.querySelector('#save-persona').click()");
        await Until(()=>JsBool("document.querySelector('#management-notice').textContent==='资料已保存。'"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#persona-name').value='';document.querySelector('#reload-persona').click()");
        await Until(()=>JsBool("document.querySelector('#persona-name').value==='桌面资料验证'"));Log("check_management_profile_save_pass");
        if(!await JsBool("document.querySelector('#persona-speaking').value==='自然交流\\n说清楚数字123' && document.querySelector('#persona-interests').value==='读书\\n绘画🙂'"))throw new IOException("profile_lists_mismatch");
        Log("check_management_profile_lists_unicode_pass");
        if(Environment.GetCommandLineArgs().Contains("--snapshot-check"))
        {
            async Task<JsonElement> ReadFixture(string name)=>await StorageComponent.Call(pipeName+"-storage",backend!,
                JsonSerializer.SerializeToElement(new{action="management",operation="records.read",
                    arguments=new{kind="memories",identifier="fact-snapshot-"+name+"-149"},request_id=Guid.NewGuid().ToString("N")}));
            var unused=await ReadFixture("unused");
            var cleanupWait=Stopwatch.StartNew();
            while(unused.GetProperty("ok").GetBoolean() && cleanupWait.ElapsedMilliseconds<75000)
            {
                await Task.Delay(250);
                unused=await ReadFixture("unused");
            }
            if(unused.GetProperty("ok").GetBoolean() || unused.GetProperty("error").GetString()!="not_found")
                throw new IOException("unused_snapshot_body_not_collected");
            var kept=await ReadFixture("kept");
            if(!kept.GetProperty("ok").GetBoolean() || kept.GetProperty("result").GetProperty("retirement").ValueKind!=JsonValueKind.Object)
                throw new IOException("referenced_snapshot_body_not_retained");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-retention-read').click()");
            await Until(()=>JsBool("document.querySelector('#fact-retention-notice').textContent.includes('自动来源记录')"));
            Log("check_snapshot_collection_retains_used_history_pass");
        }
        if(Environment.GetCommandLineArgs().Contains("--fact-check"))
        {
            await Until(()=>JsBool("[...document.querySelectorAll('#memory-items button')].some(b=>b.textContent.includes('合成事实141'))"));
            await view.CoreWebView2.ExecuteScriptAsync("[...document.querySelectorAll('#memory-items button')].find(b=>b.textContent.includes('合成事实141')).click()");
            await Until(()=>JsBool("!document.querySelector('#fact-area').hidden && !document.querySelector('#fact-refresh').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-refresh').click()");
            await Until(()=>JsBool("document.querySelector('#fact-summary').textContent.includes('冲突 2')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-items button').click();document.querySelector('#fact-clear').click()");
            if(!await JsBool("document.querySelector('#fact-confirm').disabled && document.querySelector('#fact-notice').textContent.includes('事实未改变')"))throw new IOException("fact_cancel_failed");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-items button').click();{const c=document.querySelectorAll('#fact-items input')[1];c.checked=true;c.dispatchEvent(new Event('change'));}document.querySelector('#fact-reason').value='窗口核实中文123🙂';document.querySelector('#fact-reason').dispatchEvent(new Event('input'));document.querySelector('#fact-confirm').click()");
            await Until(()=>JsBool("document.querySelector('#fact-notice').textContent.includes('确认已追加') && !document.querySelector('#fact-refresh').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-refresh').click()");
            await Until(()=>JsBool("document.querySelector('#fact-summary').textContent.includes('当前 1，冲突 0，历史 2') && document.querySelector('#fact-items').textContent.includes('窗口核实中文123🙂')"));
            if(!await JsBool("document.querySelector('#memory-content').disabled && document.querySelector('#memory-tags').disabled"))throw new IOException("fact_generic_editor_enabled");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-items button').click();document.querySelector('#fact-value').value='管理员更正142城';document.querySelector('#fact-value').dispatchEvent(new Event('input'));document.querySelector('#fact-reason').value='现场核实142🙂';document.querySelector('#fact-reason').dispatchEvent(new Event('input'))");
            if(!await JsBool("document.querySelector('#fact-confirm').disabled && !document.querySelector('#fact-correct').disabled && document.querySelector('#memory-refresh').disabled"))throw new IOException("fact_correction_controls_invalid");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-correct').click()");
            await Until(()=>JsBool("document.querySelector('#fact-notice').textContent.includes('管理员更正已保存') && !document.querySelector('#fact-refresh').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-refresh').click()");
            await Until(()=>JsBool("document.querySelector('#fact-summary').textContent.includes('当前 1，冲突 0，历史 3') && document.querySelector('#fact-items').textContent.includes('管理员更正142城') && document.querySelector('#fact-items').textContent.includes('原始来源原话（更正前）')"));
            Log("check_fact_correction_preserves_history_pass");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-area').scrollIntoView({behavior:'instant',block:'start'})");
            await Task.Delay(150);
            File.WriteAllText(Path.Combine(Program.Data,"fact-layout.json"),await view.CoreWebView2.ExecuteScriptAsync("(()=>{const f=document.querySelector('#fact-area');return {rect:f.getBoundingClientRect().toJSON(),height:innerHeight,scrollY,hidden:f.hidden};})()"));
            using(var factScreenshot=File.Create(Path.Combine(Program.Data,"fact-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,factScreenshot);
            if(!await JsBool("(()=>{const f=document.querySelector('#fact-area'),r=f.getBoundingClientRect();return !f.hidden&&r.height>0&&r.top<innerHeight&&r.bottom>0;})()"))throw new IOException("fact_timeline_not_visible");
            Log("check_fact_timeline_confirmation_pass");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-panel details').open=true;document.querySelector('#fact-retention-read').click()");
            await Until(()=>JsBool("document.querySelector('#fact-retention-days').value==='90' && !document.querySelector('#fact-retention-days').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-retention-days').value='120';document.querySelector('#fact-retention-days').dispatchEvent(new Event('input'));document.querySelector('#fact-retention-save').click()");
            await Until(()=>JsBool("document.querySelector('#fact-retention-notice').textContent.includes('清理设置已保存') && !document.querySelector('#fact-retention-read').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-retention-read').click()");
            await Until(()=>JsBool("document.querySelector('#fact-retention-days').value==='120' && document.querySelector('#fact-retention-notice').textContent.includes('设置已读取')"));
            Log("check_fact_retention_settings_pass");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-preview').click()");
            await Until(()=>JsBool("!document.querySelector('#memory-delete-confirm').disabled && document.querySelector('#memory-delete-effect').textContent.includes('不恢复旧值')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-cancel').click()");
            if(!await JsBool("document.querySelector('#memory-delete-confirm').hidden && document.querySelector('#memory-notice').textContent.includes('未改变')"))throw new IOException("fact_retirement_cancel_failed");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-preview').click()");
            await Until(()=>JsBool("!document.querySelector('#memory-delete-confirm').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-confirm').click()");
            await Until(()=>JsBool("document.querySelector('#memory-notice').textContent.includes('事实已停用') && !document.querySelector('#fact-refresh').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-refresh').click()");
            await Until(()=>JsBool("document.querySelector('#fact-summary').textContent.includes('已停用 4') && !document.querySelector('#fact-refresh').disabled"));
            if(!await JsBool("document.querySelector('#memory-delete-preview').disabled && document.querySelector('#fact-at').disabled && [...document.querySelectorAll('#fact-items button')].every(b=>b.disabled)"))throw new IOException("retired_fact_edit_enabled");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#fact-area').scrollIntoView({behavior:'instant',block:'start'})");
            await Task.Delay(150);
            using(var retiredScreenshot=File.Create(Path.Combine(Program.Data,"fact-retired-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,retiredScreenshot);
            Log("check_fact_retirement_archive_pass");
        }
        if(Environment.GetCommandLineArgs().Contains("--identity-check"))
        {
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#relation-read').click()");
            await Until(()=>JsBool("!document.querySelector('#relation-links').hidden && !document.querySelector('#relation-read').disabled"));
            foreach(var (picker,fixture) in new[]{("left","fixture-qq-114"),("right","fixture-wx-114")})
            {
                await view.CoreWebView2.ExecuteScriptAsync($"{{const box=document.querySelector('#relation-{picker}');box.value=[...box.options].find(o=>o.textContent.includes('{fixture}')).value;box.dispatchEvent(new Event('change'));}}");
            }
            async Task Preview()
            {
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#relation-review').click()");
                await Until(()=>JsBool("!document.querySelector('#relation-confirm').disabled && document.querySelector('#relation-groups').textContent.includes('fixture-wx-114')"));
            }
            await Preview();
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#relation-cancel').click()");
            await Preview();
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#relation-confirm').click()");
            await Until(()=>JsBool("document.querySelector('#relation-link-notice').textContent==='已确认是同一人。'"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#relation-review').click()");
            await Until(()=>JsBool("document.querySelector('#relation-link-notice').textContent.includes('已将两个账号识别为同一人')"));
            Log("check_manual_identity_confirmation_pass");
        }
        await Until(()=>JsBool("document.querySelector('#model-status').textContent==='尚未检查模型登录状态。' && !document.querySelector('#model-login').disabled && document.querySelector('#model-logout').disabled"));
        Log("check_model_account_default_without_network_pass");
        if(Environment.GetCommandLineArgs().Contains("--documents-check"))
        {
            foreach(var (documentKind,label) in new[]{("knowledge","知识"),("diaries","日记")})
            {
                await Until(()=>JsBool("!document.querySelector('#memory-kind').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync($"document.querySelector('#memory-kind').value='{documentKind}';document.querySelector('#memory-kind').dispatchEvent(new Event('change'))");
                await Until(()=>JsBool("document.querySelector('#memory-items').textContent.includes('合成资料147') && !document.querySelector('#memory-refresh').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-items button').click()");
                await Until(()=>JsBool("!document.querySelector('#memory-content').disabled && document.querySelector('#memory-meta').textContent.includes('已锁定自动修改')"));
                await view.CoreWebView2.ExecuteScriptAsync($"document.querySelector('#memory-content').value='窗口更正147{label}中文456🙂';document.querySelector('#memory-content').dispatchEvent(new Event('input'));document.querySelector('#memory-tags').value='核实标签147';document.querySelector('#memory-tags').dispatchEvent(new Event('input'))");
                if(!await JsBool("document.querySelector('#memory-kind').disabled && !document.querySelector('#memory-save').disabled"))throw new IOException("document_draft_controls_invalid");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-save').click()");
                await Until(()=>JsBool($"document.querySelector('#memory-notice').textContent.includes('{label}已保存') && !document.querySelector('#memory-refresh').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-refresh').click()");
                await Until(()=>JsBool($"document.querySelector('#memory-items').textContent.includes('窗口更正147{label}') && !document.querySelector('#memory-refresh').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-items button').click()");
                await Until(()=>JsBool($"document.querySelector('#memory-content').value==='窗口更正147{label}中文456🙂' && !document.querySelector('#memory-explain').disabled"));
                if(!await JsBool("document.querySelector('#memory-meta').textContent.includes('已锁定自动修改') && document.querySelector('#memory-source').textContent.includes('合成来源147') && document.querySelector('#memory-tags').value==='核实标签147'"))throw new IOException("document_metadata_changed");
                if(!await JsBool(documentKind=="knowledge"?"document.querySelector('#memory-related').textContent.includes('仅部分内容')":"document.querySelector('#memory-related').textContent.includes('synthetic-memory-147')"))throw new IOException("document_reference_missing");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-explain').click()");
                await Until(()=>JsBool("!document.querySelector('#memory-explanation').hidden && !document.querySelector('#memory-delete-preview').disabled"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-preview').click()");
                await Until(()=>JsBool("!document.querySelector('#memory-delete-cancel').disabled && !document.querySelector('#memory-delete-confirm').hidden"));
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-delete-cancel').click()");
                if(!await JsBool("document.querySelector('#memory-notice').textContent.includes('未改变')"))throw new IOException("document_cancel_invalid");
                Log("check_document_"+documentKind+"_locked_edit_pass");
            }
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#memory-panel').scrollIntoView({behavior:'instant',block:'start'})");
            await Task.Delay(150);
            using(var documentScreenshot=File.Create(Path.Combine(Program.Data,"documents-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,documentScreenshot);
        }
        if(Environment.GetCommandLineArgs().Contains("--growth-check"))
        {
            await Until(()=>JsBool("!document.querySelector('#growth-preview').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-preview').click()");
            await Until(()=>JsBool("!document.querySelector('#growth-proposal').hidden && !document.querySelector('#growth-cancel').disabled"));
            if(!await JsBool("document.querySelector('#growth-rebuild').disabled && document.querySelector('#growth-data').textContent.includes('平静')"))throw new IOException("growth_preview_invalid");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-cancel').click()");
            if(!await JsBool("document.querySelector('#growth-proposal').hidden && document.querySelector('#growth-notice').textContent.includes('未改变')"))throw new IOException("growth_cancel_invalid");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-preview').click()");
            await Until(()=>JsBool("!document.querySelector('#growth-confirm').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-confirm').click();document.querySelector('#growth-rebuild').click()");
            await Until(()=>JsBool("document.querySelector('#growth-notice').textContent.includes('公开成长已恢复') && !document.querySelector('#growth-preview').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-preview').click()");
            await Until(()=>JsBool("document.querySelector('#growth-notice').textContent.includes('无需恢复') && document.querySelector('#growth-proposal').hidden"));
            Log("check_growth_recovery_preview_confirm_pass");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-panel details').open=true;document.querySelector('#growth-archives').click()");
            await Until(()=>JsBool("document.querySelectorAll('#growth-archive-list button').length===1 && !document.querySelector('#growth-archives').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-archive-list button').click()");
            await Until(()=>JsBool("document.querySelector('#growth-archive-content').textContent.includes('归档旧状态145中文123🙂')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#growth-panel').scrollIntoView({behavior:'instant',block:'start'})");
            await Task.Delay(150);
            using(var growthScreenshot=File.Create(Path.Combine(Program.Data,"growth-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,growthScreenshot);
            Log("check_growth_archive_readonly_unicode_pass");
        }
        if(Environment.GetCommandLineArgs().Contains("--bili-policy-check"))
        {
            await Until(()=>JsBool("!document.querySelector('#bili-policy').hidden && document.querySelector('#bili-current').textContent.includes('136')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-policy').open=true");
            await Until(()=>JsBool("document.querySelectorAll('#bili-rules input:checked').length===11 && document.querySelector('[aria-label=投币日限额]').value==='2' && document.querySelector('[aria-label=\"新建收藏夹（个）日限额\"]').value==='5' && document.querySelector('[aria-label=\"移动收藏视频（个）日限额\"]').value==='50'"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('[aria-label=启用投币]').click();document.querySelector('[aria-label=投币日限额]').value='0';document.querySelector('#bili-policy-save').click()");
            await Until(()=>JsBool("document.querySelector('#bili-policy-note').textContent.includes('已保存')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-policy-read').click()");
            await Until(()=>JsBool("!document.querySelector('#bili-policy-save').disabled && document.querySelector('#bili-policy-note').textContent==='' && !document.querySelector('[aria-label=启用投币]').checked && document.querySelector('[aria-label=投币日限额]').value==='0'"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-policy-default').click()");
            await Until(()=>JsBool("document.querySelector('[aria-label=启用投币]').checked && document.querySelector('[aria-label=投币日限额]').value==='2' && document.querySelector('#bili-policy-note').textContent.includes('保存后生效')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-policy-save').click()");
            await Until(()=>JsBool("document.querySelector('#bili-policy-note').textContent.includes('已保存')"));
            Log("check_bili_live_policy_edit_pass");
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('[aria-label=\"新建收藏夹（个）日限额\"]').value='7';document.querySelector('[aria-label=\"移动收藏视频（个）日限额\"]').value='24';document.querySelector('#bili-policy-save').click()");
            await Until(()=>JsBool("document.querySelector('#bili-policy-note').textContent.includes('已保存')"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-policy-read').click()");
            await Until(()=>JsBool("!document.querySelector('#bili-policy-save').disabled && document.querySelector('[aria-label=\"新建收藏夹（个）日限额\"]').value==='7' && document.querySelector('[aria-label=\"移动收藏视频（个）日限额\"]').value==='24' && document.querySelector('[aria-label=新增收藏日限额]').value==='10'"));
            Log("check_bili_folder_independent_limits_pass");
        }
        else
            await Until(()=>JsBool("!document.querySelector('#bili-panel').hidden && document.querySelector('#bili-status').textContent==='尚未登录。' && !document.querySelector('#bili-login').disabled && document.querySelector('#bili-confirm').hidden && document.querySelector('#bili-qr').hidden"));
        Log("check_bili_account_ready_without_login_or_network_pass");
        if(Environment.GetCommandLineArgs().Contains("--bili-qr-check"))
        {
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-login').click()");
            await Until(()=>JsBool("!document.querySelector('#bili-qr').hidden && document.querySelector('#bili-qr').naturalWidth>0 && !document.querySelector('#bili-cancel').disabled"));
            await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#bili-cancel').click()");
            await Until(()=>JsBool("document.querySelector('#bili-qr').hidden && document.querySelector('#bili-status').textContent.includes('已取消') && document.querySelector('#bili-current').textContent===''"));
            Log("check_bili_live_qr_cancel_pass");
        }
        await Until(()=>JsBool("document.querySelector('#runtime-platforms').children.length===4 && [...document.querySelectorAll('#runtime-platforms button')].every(x=>x.disabled) && document.querySelector('#notification-items').textContent==='暂无运行通知。'"));
        Log("check_runtime_unavailable_controls_and_inbox_pass");
        await Until(()=>JsBool("document.querySelector('#tasks-submit').disabled && !document.querySelector('#tasks-refresh').disabled && document.querySelector('#tasks-items').textContent==='尚无手动任务。' && document.querySelector('#tasks-availability').textContent.includes('登录')"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#tasks-panel').scrollIntoView({behavior:'instant'});document.querySelector('#tasks-refresh').click()");
        await Until(()=>JsBool("document.querySelector('#tasks-items').textContent==='尚无手动任务。' && !document.querySelector('#tasks-refresh').disabled"));
        using(var tasksImage=File.Create(Path.Combine(Program.Data,"manual-tasks-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,tasksImage);
        Log("check_manual_tasks_embedded_page_and_native_empty_queue_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#runtime-panel').scrollIntoView({behavior:'instant'})");
        await Task.Delay(150);
        using(var runtimeImage=File.Create(Path.Combine(Program.Data,"runtime-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,runtimeImage);
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#lock-password').value='Synthetic-Window-Lock-2026!';document.querySelector('#enable-lock').click()");
        await Until(()=>JsBool("!document.querySelector('#lock-now').disabled"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#room-open').click()");
        await Until(()=>JsBool("document.querySelector('#room-dialog').open && document.querySelector('#room-history').textContent.includes('当前没有聊天记录')"));
        var browser=view.CoreWebView2;
        await browser.ExecuteScriptAsync("document.querySelector('#room-query').value='合成搜索';document.querySelector('#room-search').click();document.querySelector('#room-queue').open=true");
        if(Environment.GetCommandLineArgs().Contains("--room-cancel-check"))
        {
            await Until(()=>JsBool("document.querySelector('#room-history').textContent.includes('没有找到匹配') && document.querySelector('#room-jobs button')?.textContent==='取消本次回复'"));
            await browser.ExecuteScriptAsync("document.querySelector('#room-jobs button').click()");
            await Until(()=>JsBool("document.querySelector('#room-notice').textContent.includes('已取消本次回复') && document.querySelector('#room-jobs').textContent.includes('原消息已保留') && !document.querySelector('#room-jobs button')"));
            await browser.ExecuteScriptAsync("document.querySelector('#room-latest').click()");
            await Until(()=>JsBool("document.querySelector('#room-history').textContent.includes('待取消的窗口测试消息')"));
            Log("check_room_cancel_preserves_original_pass");
        }
        else
            await Until(()=>JsBool("document.querySelector('#room-history').textContent.includes('没有找到匹配') && document.querySelector('#room-jobs').textContent.includes('暂无已提交')"));
        Log("check_room_history_search_and_queue_pass");
        using(var dom=JsonDocument.Parse(await browser.CallDevToolsProtocolMethodAsync("DOM.getDocument","{}")))
        {
            int root=dom.RootElement.GetProperty("root").GetProperty("nodeId").GetInt32();
            using var picker=JsonDocument.Parse(await browser.CallDevToolsProtocolMethodAsync("DOM.querySelector",JsonSerializer.Serialize(new{nodeId=root,selector="#room-pick"})));
            int node=picker.RootElement.GetProperty("nodeId").GetInt32();
            await browser.CallDevToolsProtocolMethodAsync("DOM.setFileInputFiles",JsonSerializer.Serialize(new{nodeId=node,files=new[]{Path.Combine(Program.Data,"runtime-preview.png")}}));
        }
        await Until(()=>JsBool("document.querySelector('#room-draft-images').textContent.includes('runtime-preview.png') && !document.querySelector('#room-send').disabled"));
        await browser.ExecuteScriptAsync("document.querySelector('#room-send').click()");
        await Until(()=>JsBool("document.querySelector('#room-notice').textContent.includes('请先在平台运行中启动本机会客室') && !document.querySelector('#room-send').disabled"));
        Log("check_room_native_image_only_submit_and_retryable_draft_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#room-text').value='合成私聊草稿🙂';document.querySelector('#room-text').dispatchEvent(new Event('input'));document.querySelector('#room-lock').click()");
        await Until(()=>JsBool("!document.querySelector('#room-dialog').open && document.querySelector('#room-query').value==='' && document.querySelector('#room-jobs').children.length===0 && document.querySelector('#room-text').value==='' && document.querySelector('#room-history').children.length===0 && document.querySelector('#room-draft-images').children.length===0 && document.querySelector('#room-open').disabled"));
        Log("check_room_immediately_hidden_and_draft_cleared_pass");
        await Until(()=>JsBool("document.querySelector('#management-panel').dataset.locked==='true' && document.querySelector('#persona-name').value==='' && document.querySelector('#save-persona').disabled"));
        await Until(()=>JsBool("document.querySelector('#runtime-platforms').children.length===0 && document.querySelector('#notification-items').children.length===0 && document.querySelector('#runtime-refresh').disabled"));
        Log("check_runtime_lock_clears_management_view_pass");
        await Until(()=>JsBool("document.querySelector('#model-account').textContent==='' && document.querySelector('#model-login').disabled && document.querySelector('#model-refresh').disabled"));
        Log("check_model_account_management_lock_pass");
        await Until(()=>JsBool("!document.querySelector('#recover-lock').disabled && document.querySelector('#management-notice').textContent==='管理和会客室已锁定。'"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#recovery-area').open=true;document.querySelector('#recovery-password').value='short';document.querySelector('#recovery-confirm').value='short';document.querySelector('#recover-lock').click()");
        await Until(()=>JsBool("document.querySelector('#management-feedback').open && document.querySelector('#feedback-title').textContent==='请检查新密码' && document.querySelector('#recovery-feedback').textContent.includes('12 至 256')"));
        using(var feedbackImage=File.Create(Path.Combine(Program.Data,"recovery-feedback.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,feedbackImage);
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#feedback-close').click()");
        await Until(()=>JsBool("!document.querySelector('#management-feedback').open"));Log("check_recovery_feedback_visible_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#unlock-password').value='Incorrect-Window-Password';document.querySelector('#unlock').click()");
        await Until(()=>JsBool("document.querySelector('#management-notice').textContent==='密码不正确'"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#unlock-password').value='Synthetic-Window-Lock-2026!';document.querySelector('#unlock').click()");
        await Until(()=>JsBool("document.querySelector('#persona-name').value==='桌面资料验证' && document.querySelector('#management-panel').dataset.locked==='false'"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#lock-password').value='Synthetic-Window-Lock-2026!';document.querySelector('#disable-lock').click()");
        await Until(()=>JsBool("!document.querySelector('#enable-lock').disabled && document.querySelector('#lock-now').disabled && document.querySelector('#unlock-password').value==='' && document.querySelector('#lock-password').value===''") );
        Log("check_management_optional_lock_pass");
        await Until(()=>JsBool("document.querySelector('#runtime-platforms').children.length===4 && !document.querySelector('#runtime-refresh').disabled"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#backup-create').click()");
        await Until(()=>JsBool("document.querySelector('#backup-notice').textContent==='加密备份已保存。' && document.querySelector('#backup-selection').options.length>0"));
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#backup-preview').click()");
        await Until(()=>JsBool("!document.querySelector('#backup-restore').disabled && document.querySelector('#backup-effect').textContent.includes('覆盖当前资料')"));
        Log("check_backup_create_preview_no_windows_prompt_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#backup-panel').scrollIntoView({behavior:'instant'})");
        await Task.Delay(150);
        using(var backupImage=File.Create(Path.Combine(Program.Data,"backup-preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,backupImage);
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#management-panel').scrollIntoView()");
        await Task.Delay(150);
        using var screenshot=File.Create(Path.Combine(Program.Data,"management-preview.png"));
        await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,screenshot);
    }
    // ------------------------------------------------------------------ uninstall preparation
    // The in-app entry never starts the uninstaller itself: this window exits normally first, and
    // one helper opens the preparation page once the running marker and the database are gone.
    void BeginUninstallPreparation()
    {
        if(exiting || helperMode is not null)return;
        Log("uninstall_preparation_requested");
        helperMode = UninstallRule.OpenPreparationHelper;
        Exit();
    }
    void SkipBackup()
    {
        if(exiting || preparation is null || helperMode is not null)return;
        try { preparation.SkipBackup(); Log("uninstall_backup_skipped"); }
        catch (InvalidOperationException) { Log("uninstall_skip_rejected"); }
        PostUninstall();
    }
    // The one backup the preparation page can create: the same local encrypted backup the ordinary
    // settings area saves (backups.create), started only because the person asked for it. The
    // backend is the preparation backend, so no model and no platform is opened and no automatic
    // run can start; a profile without local data never reaches here and never gets a database.
    async Task UninstallBackup()
    {
        var session = preparation;
        if(session is null || exiting || helperMode is not null || session.Busy)return;
        if(!session.BackupAvailable){PostUninstall();return;}
        session.BeginBackup();
        PostUninstall();
        try
        {
            if(component.State!="ready")await StartBackend();
            if(component.State!="ready" || backend is null)
            {
                session.BackupFailed(component.Detail??"backend_unavailable","准备后台没有启动，因此没有创建备份。");
                Log("uninstall_backup_unavailable", new {detail=component.Detail??""});
                return;
            }
            JsonElement response;
            await control.WaitAsync(lifetime.Token);
            try
            {
                if(exiting || backend is null || component.State!="ready")throw new IOException("backup_backend_changed");
                response=await StorageComponent.Call(pipeName+"-storage",backend,JsonSerializer.SerializeToElement(new{
                    action="management",operation="backups.create",arguments=new{},request_id=Guid.NewGuid().ToString("N")}));
            }
            finally{control.Release();}
            if(response.GetProperty("ok").GetBoolean())
            {
                string name=response.TryGetProperty("result",out var result)&&result.TryGetProperty("name",out var value)
                    ? value.GetString()??"" : "";
                if(string.IsNullOrWhiteSpace(name))throw new IOException("backup_receipt_missing");
                session.BackupSucceeded(name);
                Log("uninstall_backup_completed", new {name});
            }
            else
            {
                string code=DesktopPortable.Read(response,"error")??"backup_failed";
                session.BackupFailed(code,DesktopPortable.Read(response,"message")??"");
                Log("uninstall_backup_failed", new {code});
            }
        }
        catch(Exception error) when(error is IOException or OperationCanceledException or JsonException or InvalidOperationException)
        {
            session.BackupFailed("management_unavailable","管理连接暂时不可用，没有创建备份。");
            Log("uninstall_backup_failed", new {code="management_unavailable"});
        }
        finally{PostUninstall();}
    }
    async Task UninstallPortableBackup(JsonElement message)
    {
        if(!DesktopPortable.TryPassword("portable-export",message,out var id,out _))return;
        var session=preparation!;
        if(session.Busy || exiting || !session.BackupAvailable)
        {PortableReply(id,NativeError("backup_unavailable","资料尚未就绪或上一项备份仍在进行。"));return;}
        session.BeginBackup();PostUninstall();
        try
        {
            if(component.State!="ready")await StartBackend();
            RecordPortableBackup(await PortableExport(message));
        }
        catch(Exception error) when(error is IOException or OperationCanceledException or JsonException or InvalidOperationException)
        {
            session.BackupFailed("backup_unavailable","便携备份没有完成，请重新检查后台状态。");
            PortableReply(id,NativeError("backup_unavailable",session.BackupDetail));
        }
        finally{PostUninstall();}
    }
    // A successful portable export is this run's backup as well: the same pair of files the existing
    // backup area writes, with the same password rules. A cancelled or failed export changes nothing
    // it did not change before, and never counts as a success.
    void RecordPortableBackup(JsonElement response)
    {
        var session = preparation;
        if(session is null || !Program.UninstallPrepare || helperMode is not null)return;
        bool ok=response.ValueKind==JsonValueKind.Object&&response.TryGetProperty("ok",out var flag)&&flag.GetBoolean();
        if(ok)
        {
            string name=response.TryGetProperty("result",out var result)&&result.TryGetProperty("backup",out var value)
                ? value.GetString()??"" : "";
            if(string.IsNullOrWhiteSpace(name))throw new IOException("backup_receipt_missing");
            session.BackupSucceeded(name);
            Log("uninstall_portable_backup_completed", new {name});
        }
        else
        {
            string code=DesktopPortable.Read(response,"error")??"backup_failed";
            session.BackupFailed(code,DesktopPortable.Read(response,"message")??"");
            Log("uninstall_portable_backup_failed", new {code});
        }
        PostUninstall();
    }
    // Only the person's explicit continue reaches here; the uninstaller itself is started by one
    // helper after this window, its marker and its database have closed.
    void ContinueUninstall(JsonElement message)
    {
        var session = preparation;
        if(session is null || exiting || helperMode is not null)return;
        string refusal = session.ContinueRefusal();
        if(refusal != "")
        {
            Log("uninstall_continue_refused", new { code=refusal });
            PostUninstall();
            return;
        }
        Log("uninstall_continue_confirmed", new { backup=session.Backup.ToString(), name=session.BackupName,
            entry=Path.GetFileName(session.UninstallerPath!) });
        bool clear=message.TryGetProperty("clear_profile",out var clearValue)&&clearValue.ValueKind==JsonValueKind.True;
        if(clear && !ProfileCleanup.IsDefault(session.DataRoot)){PostUninstall();return;}
        helperMode = clear ? UninstallRule.FinishClearHelper : UninstallRule.FinishHelper;
        Exit();
    }
    // One state document for the preparation page: what the entry is, what the last real result
    // was, whether continuing is allowed right now, and where personal data and backups live.
    string UninstallBackupFolder()
    {
        string config=Path.Combine(Program.Paths.Data,"installation","storage-config.json");
        if(!File.Exists(config))return "";
        try
        {
            using var document=JsonDocument.Parse(File.ReadAllText(config));
            string? data=document.RootElement.TryGetProperty("data",out var value) && value.ValueKind==JsonValueKind.String?value.GetString():null;
            return string.IsNullOrWhiteSpace(data)?"":Path.Combine(data,"history","backups");
        }
        catch(Exception error) when(error is IOException or UnauthorizedAccessException or JsonException){return "";}
    }
    async Task UninstallBackupSmoke()
    {
        await Until(()=>Task.FromResult(loaded));
        await Until(()=>JsBool("document.querySelector('#uninstall-retention').textContent.length>10 && !document.querySelector('#backup-create').disabled"));
        if(component.State=="ready" || backend is not null)throw new IOException("uninstall_auto_started_backend");
        Log("check_uninstall_no_automatic_backend_pass");
        await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#backup-create').click()");
        Log("uninstall_smoke_clicked",new{page=await view.CoreWebView2.ExecuteScriptAsync("JSON.stringify({status:document.querySelector('#uninstall-status').textContent,notice:document.querySelector('#backup-notice').textContent,handler:typeof document.querySelector('#backup-create').onclick})")});
        await Until(()=>Task.FromResult(preparation!.Backup is UninstallBackupState.Succeeded or UninstallBackupState.Failed),120000);
        if(preparation!.Backup!=UninstallBackupState.Succeeded)throw new IOException(preparation.BackupDetail);
        if(!File.Exists(Path.Combine(UninstallBackupFolder(),preparation.BackupName)))throw new IOException("backup_file_missing");
        var status=await StorageComponent.Call(pipeName+"-storage",backend!,JsonSerializer.SerializeToElement(new{
            action="management",operation="runtime.status",arguments=new{},request_id=Guid.NewGuid().ToString("N")}));
        foreach(var platform in status.GetProperty("result").GetProperty("platforms").EnumerateArray())
            if(platform.GetProperty("model_ready").GetBoolean() || platform.GetProperty("available").GetBoolean()
                || platform.GetProperty("status").GetString()=="active")throw new IOException("backup_opened_business_runtime");
        Log("check_uninstall_real_encrypted_backup_without_platform_or_model_pass");
        await Task.Delay(300);
        using(var picture=File.Create(Path.Combine(Program.Data,"uninstall-preview.png")))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,picture);
        SaveResult(true);Exit();
    }
    void PostUninstall()
    {
        var session = preparation;
        Post(new { type="uninstall", state=new {
            mode=Program.UninstallPrepare?"prepare":"closed",
            registered=session?.Registered??false,
            uninstaller=session?.UninstallerPath??"",
            entry=session?.UninstallerPath is string path ? Path.GetFileName(path) : "",
            application=Program.Paths.ApplicationDirectory,
            profile=Program.Paths.Root,
            can_clear_profile=ProfileCleanup.IsDefault(Program.Paths.Root),
            backup_folder=UninstallBackupFolder(),
            backup_available=session?.BackupAvailable??false,
            backup_reason=session?.BackupUnavailableReason??"",
            backup=session?.Backup.ToString().ToLowerInvariant()??"none",
            backup_name=session?.BackupName??"",
            backup_detail=session?.BackupDetail??"",
            continue_refusal=session?.ContinueRefusal()??"uninstaller_missing",
            handed_off=session?.HandedOff??false,
            guidance=UninstallRule.PortableDirectoryGuidance,
            backend=component.State, detail=component.Detail??"" } });
    }
    bool StartHelper(string mode)
    {
        var start = new ProcessStartInfo(Environment.ProcessPath!)
            { UseShellExecute=false, WorkingDirectory=Program.Paths.ApplicationDirectory };
        start.ArgumentList.Add(mode);start.ArgumentList.Add(Environment.ProcessId.ToString());
        if(mode==UninstallRule.OpenPreparationHelper)start.ArgumentList.Add(Program.Paths.Root);
        try { using var process=Process.Start(start); return process is not null; }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception
            or InvalidOperationException or ObjectDisposedException or IOException) { return false; }
    }
    async void Exit()
    {
        if (exiting) return;
        exiting=true; poll.Stop(); lifetime.Cancel();
        RestoreWindow();
        view.Visible=false;fallback.Visible=false;recoveryStatus.Visible=false;
        shutdownText.Text="正在停止任务、保存资料并退出……\n请稍候，数据库仍在关闭时不会自动强制结束。";
        shutdownPanel.Visible=true;shutdownPanel.BringToFront();
        using var finished=new CancellationTokenSource();
        _ = ShowForceExit(finished.Token);
        bool graceful=false;
        try
        {
            if(journalStarted)journal.Closing();
            if(portablePending || portableBusy || portableActive)await PortableCancelOnExit();
            await StopBackend();
            Log("shutdown_completed", new {state=component.State, detail=component.Detail});
            graceful=component.Detail=="graceful_stop";
            if(!graceful)Result=1;
            if(journalStarted)journal.Complete(graceful);
        }
        catch (Exception error) { Result=1;if(journalStarted)journal.Abnormal(); Log("shutdown_failed", new {error.Message}); }
        finally
        {
            // The uninstaller is never started by this window: only a successful normal shutdown
            // lets one helper start it, after the running marker and the database are gone. A
            // forced exit, a failed close or a cancel therefore never starts an uninstall.
            if(graceful && helperMode is not null)
            {
                preparation?.MarkHandedOff();
                if(StartHelper(helperMode))Log("uninstall_helper_started", new {mode=helperMode});
                else{Result=1;Log("uninstall_helper_failed", new {mode=helperMode});}
            }
            finished.Cancel();shutdownComplete=true; Close();
        }
    }
    async Task ShowForceExit(CancellationToken token)
    {
        try { await Task.Delay(TimeSpan.FromSeconds(10),token); }
        catch(OperationCanceledException) { return; }
        shutdownText.Text="仍在保存并退出。可以继续等待。\n强制退出会中断关闭；下次启动将检查恢复，未完成操作可能需要重新处理。";
        forceExit.Visible=true;
        Log("shutdown_waiting_for_user");
    }
    void ForceExit()
    {
        if(!exiting || shutdownComplete)return;
        forceExit.Enabled=false;shutdownText.Text="正在强制退出；下次启动将检查恢复……";
        if(journalStarted)journal.Abnormal(forced:true);
        Log("shutdown_force_requested");
        component.RequestForceStop();
    }
    protected override void Dispose(bool disposing)
    {
        if(disposing){exiting=true;lifetime.Cancel();poll.Stop();component.Dispose();tray.Visible=false;tray.Dispose();view.Dispose();poll.Dispose();}
        base.Dispose(disposing);
    }
}
