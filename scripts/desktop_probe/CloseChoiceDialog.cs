// SPDX-License-Identifier: MIT
namespace Qiban.DesktopProbe;

internal sealed class CloseChoiceDialog : Form
{
    internal CloseChoiceDialog()
    {
        Text="关闭栖伴？";FormBorderStyle=FormBorderStyle.FixedDialog;
        StartPosition=FormStartPosition.CenterParent;ShowInTaskbar=false;
        MaximizeBox=MinimizeBox=false;ClientSize=new Size(480,182);
        Font=new Font("Microsoft YaHei UI",10);BackColor=Color.FromArgb(250,247,253);
        var text=new Label{Text="关闭窗口后，你可以让栖伴在后台继续运行，或完全退出。退出应用会终止正在运行的任务。",
            AutoSize=false,Location=new Point(24,24),Size=new Size(432,76)};
        var buttons=new FlowLayoutPanel{FlowDirection=FlowDirection.RightToLeft,
            Location=new Point(20,115),Size=new Size(440,48)};
        var cancel=new Button{Text="取消",DialogResult=DialogResult.Cancel,Size=new Size(100,38)};
        var background=new Button{Text="后台运行",DialogResult=DialogResult.Ignore,Size=new Size(116,38)};
        var exit=new Button{Text="退出应用",DialogResult=DialogResult.OK,Size=new Size(116,38)};
        buttons.Controls.AddRange([cancel,background,exit]);Controls.AddRange([text,buttons]);
        CancelButton=cancel;ActiveControl=cancel;
    }
}
