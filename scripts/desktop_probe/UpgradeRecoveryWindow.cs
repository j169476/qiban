// SPDX-License-Identifier: MIT
using System.Diagnostics;

namespace Qiban.DesktopProbe;

// --upgrade-recovery <installation-root> <installed-app>
//
// Dispatched before the ordinary GUI running marker is created: a rollback is refused while that
// marker is held, and this window must not start business, the database or the WebView at all.
// It also runs from the transaction's own carrier copy - the previous program - because the
// installed directory is exactly what a rollback overwrites. A program started anywhere else hands
// the request over to that carrier and exits without running any maintenance of its own.
internal static class UpgradeRecovery
{
    internal static int Run(string? root,string? installed)
    {
        var record=UpgradeRecoveryRecord.Read(root,installed);
        if(record.CanRollback && !record.IsCarrier)
        {
            // A carrier never hands over to itself: that can only mean the wrong program was run.
            if(Environment.GetEnvironmentVariable(UpgradeRecoveryRecord.CarrierMarker)=="1")
                return Show(record.Refuse("维护窗口没有从独立载体启动，本次没有执行任何操作。"));
            return HandOver(record);
        }
        return Show(record);
    }

    static int HandOver(UpgradeRecoveryRecord record)
    {
        string? carrier=record.Carrier;
        string? executable=null;
        if(carrier is not null)
        {
            string name=Path.GetFileName(Environment.ProcessPath??"");
            if(name.Length>0)executable=Path.Combine(carrier,name);
            if(executable is null || !File.Exists(executable))executable=Path.Combine(carrier,"Qiban.exe");
            if(!File.Exists(executable))executable=null;
        }
        if(carrier is null || executable is null)
            return Show(record.Refuse("维护载体缺少可启动的程序，本次没有执行任何操作。"));
        try
        {
            var info=new ProcessStartInfo(executable){UseShellExecute=false,WorkingDirectory=carrier};
            info.ArgumentList.Add("--upgrade-recovery");
            info.ArgumentList.Add(record.Root);
            info.ArgumentList.Add(record.Installed);
            info.Environment[UpgradeRecoveryRecord.CarrierMarker]="1";
            // This process exits now, on purpose: it is the program the rollback will overwrite, and
            // Windows keeps a running image open for writing. Waiting here for the carrier window
            // would make the installed Qiban.exe un-writable and fail the very step it started.
            var started=Process.Start(info);
            if(started is null)return Show(record.Refuse("维护载体未能启动，本次没有执行任何操作。"));
            started.Dispose();
            return 0;
        }
        catch(Exception error) when(error is System.ComponentModel.Win32Exception or InvalidOperationException
            or IOException)
        {
            return Show(record.Refuse("维护载体未能启动，本次没有执行任何操作。"));
        }
    }

    static int Show(UpgradeRecoveryRecord record)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using var window=new UpgradeRecoveryWindow(record);
        Application.Run(window);
        return window.Result;
    }
}

// One native window, no page and no browser: the explanation, the two ways back, and a close
// button. Every maintenance step runs behind one Windows confirmation, so a person reads one
// prompt per task instead of one per internal step, and a second press during a task does nothing.
//
// The explanation and the status are wrapped labels inside their own scroll areas, above a footer
// the window never covers: a window size or a display scaling that no longer fits the text makes
// it scroll instead of cutting its last paragraph, and the three buttons stay where they are at
// every size. The wording, the steps and the single Windows verification do not depend on size.
internal sealed class UpgradeRecoveryWindow : Form
{
    const string Intro="这个窗口把栖伴退回到升级前的程序与资料。窗口不会启动栖伴的业务后台、"
        +"数据库服务，也不会打开应用页面。\n\n"
        +"常规回退：先由新版本保全升级后的资料，再由维护载体切回升级前的程序与资料；"
        +"本机设置沿用升级后的最新状态。\n"
        +"紧急回退：新版维护后台无法运行时，先封存升级后的故障资料，再回到升级前的程序和资料，"
        +"应用设置回到升级前的状态；升级后的内容暂时不会出现。\n\n"
        +"开始前请确认栖伴已经退出。回退完成后需要重新打开栖伴；所有平台保持停用。";

    // Below this the frame is refused: one row of buttons plus enough text to read before scrolling.
    const int LeastTextWidth=160,LeastTextHeight=96;

    readonly UpgradeRecoveryRecord record;
    readonly Font font=new("Microsoft YaHei UI",10F);
    readonly Label explanation=new(){AutoSize=true,MaximumSize=new Size(LeastTextWidth,0),TextAlign=ContentAlignment.TopLeft};
    readonly Label status=new(){AutoSize=true,MaximumSize=new Size(LeastTextWidth,0),TextAlign=ContentAlignment.TopLeft};
    readonly Panel explanationArea=new(){Dock=DockStyle.Fill,AutoScroll=true};
    readonly Panel statusArea=new(){Dock=DockStyle.Fill,AutoScroll=true};
    readonly Button ordinary=new(){Text="常规回退",AutoSize=true,MinimumSize=new Size(128,38)};
    readonly Button emergency=new(){Text="紧急回退",AutoSize=true,MinimumSize=new Size(128,38)};
    readonly Button close=new(){Text="关闭",AutoSize=true,MinimumSize=new Size(96,38)};
    readonly FlowLayoutPanel buttons;
    readonly TableLayoutPanel layout;
    readonly CancellationTokenSource lifetime=new();
    CancellationTokenSource? work;
    bool busy, finishing, completed;
    internal int Result;

    internal UpgradeRecoveryWindow(UpgradeRecoveryRecord record)
    {
        this.record=record;
        Text="栖伴 · 升级回退";
        Font=font;
        // The footer rows and the wrapped text follow the display's own scaling, so a scaled
        // desktop gets readable lines instead of rows sized for 100%.
        AutoScaleMode=AutoScaleMode.Dpi;
        AutoScaleDimensions=new SizeF(96F,96F);
        ClientSize=new Size(700,486);
        MinimumSize=new Size(520,400);
        StartPosition=FormStartPosition.CenterScreen;
        // A resizable frame with no maximize button: a person can give the text more room, and
        // nothing about the window's job changes with its size.
        FormBorderStyle=FormBorderStyle.Sizable;
        MaximizeBox=false;
        MinimizeBox=false;
        explanation.Text=Intro;
        explanationArea.Controls.Add(explanation);
        statusArea.Controls.Add(status);
        buttons=new FlowLayoutPanel{Dock=DockStyle.Fill,FlowDirection=FlowDirection.RightToLeft,
            WrapContents=false,Padding=new Padding(0,10,0,0)};
        buttons.Controls.Add(close);
        buttons.Controls.Add(emergency);
        buttons.Controls.Add(ordinary);
        layout=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=1,RowCount=3,
            Padding=new Padding(20,18,20,14)};
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100F));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent,100F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute,104F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute,56F));
        layout.Controls.Add(explanationArea,0,0);
        layout.Controls.Add(statusArea,0,1);
        layout.Controls.Add(buttons,0,2);
        Controls.Add(layout);
        explanationArea.SizeChanged+=(_,_)=>Wrap();
        statusArea.SizeChanged+=(_,_)=>Wrap();
        ordinary.Click+=async(_,_)=>await Execute(false);
        emergency.Click+=async(_,_)=>await Execute(true);
        close.Click+=(_,_)=>Close();
        SetButtons(true);
        ShowState();
    }

    // A wrapped label grows downwards only when it is given a maximum width, and that growth is
    // what makes its area scroll instead of cutting the text. The scrollbar's width is always
    // reserved, so the wrap point stays the same whether or not the bar is showing.
    void Wrap()
    {
        Wrapped(explanation,explanationArea);
        Wrapped(status,statusArea);
    }

    static void Wrapped(Label label,Control area)
    {
        int width=Math.Max(LeastTextWidth,area.ClientSize.Width-SystemInformation.VerticalScrollBarWidth-2);
        if(label.MaximumSize.Width!=width)label.MaximumSize=new Size(width,0);
    }

    // The frame minimum is measured, not assumed: one row of buttons has to fit at whatever width
    // the display scaling gives them, and the text keeps a few lines before it scrolls.
    void FitMinimum()
    {
        int width=layout.Padding.Horizontal+buttons.Padding.Horizontal+Width-ClientSize.Width;
        foreach(Control button in buttons.Controls)width+=button.Width+button.Margin.Horizontal;
        int height=layout.Padding.Vertical+LeastTextHeight+Height-ClientSize.Height
            +(int)layout.RowStyles[1].Height+(int)layout.RowStyles[2].Height;
        MinimumSize=new Size(Math.Max(520,width),Math.Max(400,height));
    }

    // Both labels are read from their first line: a new message must not start where the last one
    // was scrolled to.
    void SetStatus(string text)
    {
        status.Text=text;
        if(statusArea.IsHandleCreated)statusArea.AutoScrollPosition=new Point(0,0);
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        Wrap();
        FitMinimum();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        Wrap();
        FitMinimum();
    }

    // A record that offers nothing says why; a record that offers one path says what the other
    // one is waiting for, instead of failing after the person already pressed it.
    void ShowState()
    {
        if(!record.CanRollback)
        {
            SetStatus(record.Reason??"现在没有可回退的升级。");
            return;
        }
        var notes=new List<string>();
        if(record.NormalBlocker is not null)notes.Add("常规回退不可用："+record.NormalBlocker);
        if(record.EmergencyBlocker is not null)notes.Add("紧急回退不可用："+record.EmergencyBlocker);
        SetStatus(notes.Count==0
            ?"请选择“常规回退”或“紧急回退”。开始后请在 Windows 提示中验证身份。"
            :string.Join("\n",notes));
    }

    void SetButtons(bool enabled)
    {
        ordinary.Enabled=enabled && record.CanRollback && record.NormalBlocker is null;
        emergency.Enabled=enabled && record.CanRollback && record.EmergencyBlocker is null;
        close.Enabled=true;
    }

    async Task Execute(bool escalated)
    {
        if(busy || completed || !record.CanRollback)return;
        busy=true;
        SetButtons(false);
        SetStatus("请在 Windows 提示中验证当前 Windows 身份……");
        var running=CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        work=running;
        try
        {
            // One confirmation for the whole task, then the verbs of the record's own plan in the
            // program each of them must run in: preservation in the new program, the switch back
            // and the emergency seal in the carrier.
            var flow=new UpgradeRecoveryFlow(token=>WindowsVerification.VerifyUpgrade(Handle,token,escalated),
                (verb,token)=>UpgradeRecoveryHost.Execute(
                    UpgradeRecoveryRecord.Worker(verb=="rollback-prepare"?record.Installed:record.Carrier!),
                    record.Root,record.Installed,verb,token),
                text=>{if(!IsDisposed)SetStatus(text);});
            var outcome=await flow.Run(escalated,record.Steps(escalated),running.Token);
            SetStatus(outcome.Message);
            if(outcome.Success)completed=true;
            // Any task that was attempted and did not succeed reports failure to its caller, even
            // when the backend refused before touching anything.
            Result=outcome.Success?0:1;
        }
        catch(Exception error) when(error is IOException or OperationCanceledException
            or System.ComponentModel.Win32Exception or System.Text.Json.JsonException
            or InvalidOperationException or TimeoutException or System.Security.SecurityException)
        {
            // Neither backend text nor an exception message is shown: one fixed uncertain result.
            Result=1;
            SetStatus(UpgradeRecoveryFlow.Unknown);
        }
        finally
        {
            if(ReferenceEquals(work,running))work=null;
            running.Dispose();
            busy=false;
            if(!completed && !finishing)SetButtons(true);
            if(finishing)Close();
        }
    }

    // Closing while a maintenance step runs cancels it and kills the backend it started, so this
    // window never leaves a worker of its own behind.
    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if(busy && !finishing)
        {
            e.Cancel=true;
            finishing=true;
            SetStatus("正在停止维护操作，请稍候……");
            try{work?.Cancel();}catch(ObjectDisposedException){}
            return;
        }
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if(disposing)
        {
            try{work?.Cancel();}catch(ObjectDisposedException){}
            work?.Dispose();
            work=null;
            lifetime.Dispose();
        }
        base.Dispose(disposing);
        if(disposing)font.Dispose();
    }
}
