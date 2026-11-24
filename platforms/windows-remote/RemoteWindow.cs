using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Qiban.Remote
{
    internal sealed class RemoteWindow : Form
    {
        private readonly TextBox host = new TextBox(), identity = new TextBox(), trust = new TextBox();
        private readonly NumericUpDown port = new NumericUpDown { Minimum = 1, Maximum = 65535, Value = 22 };
        private readonly Button loadProfile = new Button { Text = "载入连接文件…", AutoSize = true, Name = "LoadProfile" };
        private readonly Button connect = new Button { Text = "连接", AutoSize = true, Name = "Connect" };
        private readonly Button disconnect = new Button { Text = "断开", AutoSize = true, Name = "Disconnect" };
        private readonly TableLayoutPanel fields = new TableLayoutPanel();
        internal readonly StatusView View = new StatusView();
        private StatusLink link;
        private int generation;
        private bool active;
        internal int SuccessfulConnections { get; private set; }
        internal string State { get; private set; }
        internal string FailureCode { get; private set; }
        internal Dictionary<string,object> LastSample { get; private set; }

        internal RemoteWindow(Dictionary<string,string> options)
        {
            Text = "栖伴 · Linux 远程控制台";
            Font = new Font("Microsoft YaHei UI", 9F);
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(820, 710);
            MinimumSize = new Size(690, 570);
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, Padding = new Padding(12) };
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            Controls.Add(layout);
            fields.ColumnCount = 3;
            fields.Dock = DockStyle.Fill;
            fields.AutoSize = true;
            fields.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            fields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            fields.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.Controls.Add(fields, 0, 0);
            host.Name = "Host"; identity.Name = "IdentityFile"; trust.Name = "KnownHostsFile"; port.Name = "Port";
            host.AccessibleName = "Linux 主机"; port.AccessibleName = "SSH 端口";
            identity.AccessibleName = "客户端密钥"; trust.AccessibleName = "服务器公钥文件";
            AddField(0, "Linux 主机", host);
            port.Width = 80;
            var ports = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = Padding.Empty };
            ports.Controls.Add(new Label { Text = "SSH 端口", AutoSize = true, Anchor = AnchorStyles.Left });
            ports.Controls.Add(port);
            fields.Controls.Add(ports, 2, 0);
            AddFile(1, "客户端密钥", identity);
            AddFile(2, "服务器公钥文件", trust);
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            buttons.Controls.Add(loadProfile);
            loadProfile.Click += (s,e) => {
                using (var dialog = new OpenFileDialog { Title = "载入受信任的服务器连接文件", Filter = "连接文件 (*.json)|*.json" }) {
                    if (dialog.ShowDialog(this) == DialogResult.OK) LoadProfile(dialog.FileName);
                }
            };
            buttons.Controls.Add(connect); buttons.Controls.Add(disconnect);
            layout.Controls.Add(buttons, 0, 1);
            View.Dock = DockStyle.Fill; layout.Controls.Add(View, 0, 2);
            connect.Click += (s,e) => Connect();
            disconnect.Click += (s,e) => Disconnect();
            FormClosing += (s,e) => StopTransport();
            string arg;
            if (options.TryGetValue("host", out arg)) host.Text = arg;
            if (options.TryGetValue("identity", out arg)) identity.Text = arg;
            if (options.TryGetValue("known-hosts", out arg)) trust.Text = arg;
            if (options.TryGetValue("port", out arg)) port.Value = Int32.Parse(arg);
            SetState("disconnected", "选择已登记的客户端密钥和从服务器核对过的 known_hosts 文件。");
            if (options.TryGetValue("profile", out arg)) LoadProfile(arg);
        }

        private void LoadProfile(string path)
        {
            try {
                var settings = EndpointProfile.Load(path);
                host.Text = settings.Host; port.Value = settings.Port;
                identity.Text = settings.Identity; trust.Text = settings.KnownHosts;
                FailureCode = null;
                SetState("disconnected", "已载入连接文件。点击连接以验证服务器身份。");
            } catch (Exception e) {
                // A rejected import must never leave a previous target ready for an accidental connect.
                host.Clear(); identity.Clear(); trust.Clear();
                FailureCode = "settings"; SetState("error", "无法载入连接文件。" + e.Message);
            }
        }

        private void AddField(int row, string name, Control field)
        {
            fields.Controls.Add(new Label { Text = name, AutoSize = true, Anchor = AnchorStyles.Left }, 0, row);
            field.Dock = DockStyle.Fill;
            fields.Controls.Add(field, 1, row);
        }

        private void AddFile(int row, string name, TextBox field)
        {
            AddField(row, name, field);
            var browse = new Button { Text = "选择文件…", AutoSize = true, AccessibleName = "选择" + name };
            browse.Click += (s,e) => {
                using (var dialog = new OpenFileDialog { Title = "选择" + name, CheckFileExists = true })
                    if (dialog.ShowDialog(this) == DialogResult.OK) field.Text = dialog.FileName;
            };
            fields.Controls.Add(browse, 2, row);
        }

        private void SetState(string state, string detail)
        {
            State = state;
            fields.Enabled = !active; loadProfile.Enabled = !active;
            connect.Enabled = !active; disconnect.Enabled = active;
            View.SetConnection(state, detail);
        }

        private void StopTransport()
        {
            generation++; active = false;
            var old = link; link = null;
            if (old != null) old.Dispose();
        }

        private void Connect()
        {
            if (active) return;
            FailureCode = null;
            ConnectionSettings settings;
            try { settings = new ConnectionSettings(host.Text, (int)port.Value, identity.Text, trust.Text); }
            catch (Exception e) { FailureCode = "settings"; SetState("error", e.Message); return; }
            active = true;
            Run(settings, ++generation);
        }

        private void Disconnect()
        {
            StopTransport();
            SetState("disconnected", "管理连接已断开。Linux 后台继续运行。");
        }

        private async void Run(ConnectionSettings settings, int epoch)
        {
            int retryMs = 1000;
            bool retry = false;
            while (epoch == generation) {
                SetState(retry ? "reconnecting" : "connecting", retry ? "正在重新验证服务器身份…" : "正在验证服务器身份…");
                StatusLink session = null;
                try {
                    session = new StatusLink(settings);
                    link = session;
                    await session.Negotiate();
                    bool first = true;
                    while (epoch == generation) {
                        var sample = await session.Snapshot();
                        if (epoch != generation) return;
                        if (first) {
                            first = false; SuccessfulConnections++; retryMs = 1000;
                            SetState("connected", "已验证服务器身份 · 只读状态");
                        }
                        LastSample = sample; View.SetStatus(sample);
                        await Task.Delay(1500);
                    }
                    return;
                } catch (Exception e) {
                    if (epoch != generation) return;
                    var failure = e as LinkFailure;
                    if (failure == null || !failure.CanRetry) {
                        active = false;
                        FailureCode = failure == null ? "local_error" : failure.Code;
                        SetState("error", failure != null || e is ArgumentException ? e.Message : "本机无法启动或读取 SSH 客户端。");
                        return;
                    }
                    SetState("reconnecting", "连接中断，将在 " + (retryMs / 1000) + " 秒后重连。");
                } finally {
                    if (ReferenceEquals(link, session)) link = null;
                    if (session != null) session.Dispose();
                }
                retry = true;
                await Task.Delay(retryMs);
                retryMs = Math.Min(15000, retryMs * 2);
            }
        }

        // The acceptance driver uses the same controls, session and renderer as a user.
        internal void ConnectForCheck() { connect.PerformClick(); }
        internal void DisconnectForCheck() { disconnect.PerformClick(); }
        internal void InterruptForCheck() { link.Interrupt(); }
        internal void SetTrustForCheck(string file) { trust.Text = file; }
    }
}
