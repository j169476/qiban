using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Windows.Forms;

namespace Qiban.Remote
{
    public sealed class StatusView : UserControl
    {
        private const string Missing = "未提供";
        private readonly TableLayoutPanel rows = new TableLayoutPanel();
        private readonly Dictionary<string, Label> values = new Dictionary<string, Label>();
        private readonly Label syntheticTitle = TextLabel("测试会话");
        private readonly Font headingFont;
        private bool hasSample;
        private bool fresh;
        private string connection = "disconnected";
        private string installation;
        private string instance;
        private string continuity;

        public StatusView()
        {
            Name = "StatusView";
            AccessibleName = "服务器状态";
            AutoScroll = true;
            BackColor = SystemColors.Window;
            ForeColor = SystemColors.WindowText;
            headingFont = new Font("Microsoft YaHei UI", 9, FontStyle.Bold);
            rows.Dock = DockStyle.Top;
            rows.AutoSize = true;
            rows.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            rows.Padding = new Padding(12, 2, 12, 12);
            rows.ColumnCount = 2;
            rows.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute,
                TextRenderer.MeasureText("最近完成任务", headingFont).Width + 22));
            rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            Controls.Add(rows);

            Section("服务器连接");
            Field("连接状态", "ConnectionState");
            Field("连接详情", "ConnectionDetail");
            Section("最近采样");
            Field("采样状态", "SampleState");
            Field("采样时间", "GeneratedAt");
            Field("安装标识", "InstallationId");
            Field("运行实例", "RuntimeInstance");
            Field("后台状态", "ServerStatus");
            Field("本次心跳", "HeartbeatCount");
            int dialogueRow = NewRow();
            syntheticTitle.Name = "SyntheticDialogueTitle";
            rows.Controls.Add(syntheticTitle, 0, dialogueRow);
            rows.Controls.Add(Value("SyntheticDialogue", "测试会话计数"), 1, dialogueRow);
            ShowSyntheticDialogue(null);
            Section("文档索引任务");
            AddCounts();
            Field("最近完成任务", "LastCompletedId");
            Section("平台能力");
            Field("会客室", "PlatformLocal");
            Field("QQ", "PlatformQq");
            Field("微信", "PlatformWechat");
            Field("哔哩哔哩", "PlatformBilibili");

            rows.SizeChanged += delegate { WrapValues(); };
            WrapValues();
            SetConnection("disconnected", null);
        }

        private static Label TextLabel(string text)
        {
            return new Label { Text = text, AutoSize = true, UseMnemonic = false,
                Anchor = AnchorStyles.Left | AnchorStyles.Top, Margin = new Padding(0, 2, 0, 2) };
        }

        private int NewRow()
        {
            int index = rows.RowCount++;
            rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            return index;
        }

        private void Section(string title)
        {
            Label label = TextLabel(title);
            label.Font = headingFont;
            label.Margin = new Padding(0, 10, 0, 5);
            rows.Controls.Add(label, 0, NewRow());
            rows.SetColumnSpan(label, 2);
        }

        private Label Value(string name, string title)
        {
            Label label = TextLabel("—");
            label.Name = name;
            label.AccessibleName = title;
            values.Add(name, label);
            return label;
        }

        private void Field(string title, string name)
        {
            int row = NewRow();
            rows.Controls.Add(TextLabel(title), 0, row);
            rows.Controls.Add(Value(name, title), 1, row);
        }

        private void AddCounts()
        {
            var counts = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Fill,
                ColumnCount = 4, RowCount = 2, Margin = Padding.Empty };
            string[] titles = { "排队中", "运行中", "已完成", "失败" };
            string[] keys = { "Queued", "Running", "Completed", "Failed" };
            for (int i = 0; i < titles.Length; i++)
            {
                counts.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));
                counts.Controls.Add(TextLabel(titles[i]), i, 0);
                counts.Controls.Add(Value(keys[i], titles[i] + "任务数"), i, 1);
            }
            rows.Controls.Add(counts, 0, NewRow());
            rows.SetColumnSpan(counts, 2);
        }

        private void WrapValues()
        {
            int width = Math.Max(1, rows.ClientSize.Width - rows.Padding.Horizontal -
                (int)rows.ColumnStyles[0].Width - 2);
            foreach (Label label in values.Values)
                if (label.Parent == rows && label.MaximumSize.Width != width)
                    label.MaximumSize = new Size(width, 0);
        }

        public void SetConnection(string state, string detail)
        {
            connection = state;
            string text;
            switch (state)
            {
                case "disconnected": text = "已断开"; break;
                case "connecting": text = "正在连接"; break;
                case "connected": text = "已连接"; break;
                case "reconnecting": text = "正在重连"; break;
                case "error": text = "连接错误"; break;
                default: text = "连接状态未知"; break;
            }
            if (state != "connected") fresh = false;
            values["ConnectionState"].Text = text;
            values["ConnectionDetail"].Text = String.IsNullOrWhiteSpace(detail) ? "—" : detail;
            RenderSampleState();
        }

        private void RenderSampleState()
        {
            string text;
            if (!hasSample) text = "尚无采样";
            else if (connection != "connected") text = "离线 · 上次采样";
            else if (!fresh) text = "等待新采样 · 显示上次采样";
            else text = String.IsNullOrEmpty(continuity) ? "已采样" : "已采样 · " + continuity;
            values["SampleState"].Text = text;
        }

        private static object Get(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) ? value : null;
        }

        private static string Reported(Dictionary<string, object> data, string key)
        {
            string value = Get(data, key) as string;
            return String.IsNullOrWhiteSpace(value) ? null : value;
        }

        private static string Count(Dictionary<string, object> data, string key)
        {
            object value = Get(data, key);
            if (!(value is int || value is long || value is decimal)) return Missing;
            decimal number = Convert.ToDecimal(value, CultureInfo.InvariantCulture);
            return number >= 0 && number == Decimal.Truncate(number)
                ? number.ToString("0", CultureInfo.InvariantCulture) : Missing;
        }

        private void ShowSyntheticDialogue(Dictionary<string, object> dialogue)
        {
            bool supported = Reported(dialogue, "backend") == "postgresql" &&
                Reported(dialogue, "mode") == "synthetic";
            Label value = values["SyntheticDialogue"];
            value.Text = supported ? String.Join(" · ", new[] {
                "已确认 " + Count(dialogue, "confirmed"),
                "回执不明 " + Count(dialogue, "uncertain"),
                "实际提交 " + Count(dialogue, "submissions"),
                "拒绝 " + Count(dialogue, "denied"),
                "重复 " + Count(dialogue, "replays")
            }) : String.Empty;
            syntheticTitle.Visible = value.Visible = supported;
        }

        private static string SampleTime(Dictionary<string, object> snapshot)
        {
            string raw = Reported(snapshot, "generated_at");
            DateTimeOffset at;
            if (raw == null) return Missing;
            if (!raw.EndsWith("Z", StringComparison.Ordinal) ||
                !DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture, DateTimeStyles.None, out at))
                return "时间格式无效";
            return at.UtcDateTime.ToString("yyyy-MM-dd HH:mm:ss.FFFFFFF", CultureInfo.InvariantCulture) + " UTC";
        }

        private static string State(string value)
        {
            switch (value)
            {
                case "running": return "运行中";
                case "unavailable": return "不可用";
                case "paused": return "已暂停";
                case "stopped": return "已停止";
                default: return value ?? Missing;
            }
        }

        private static string Platform(Dictionary<string, object> platforms, string key)
        {
            var item = Get(platforms, key) as Dictionary<string, object>;
            if (item == null) return Missing;
            object available = Get(item, "available");
            string capability = available is bool ? ((bool)available ? "可用" : "不可用") : "可用性未提供";
            string status = State(Reported(item, "status"));
            string reason = Reported(item, "reason");
            string summary = capability;
            if (status != capability)
                summary += status == Missing ? " · 状态未提供" : " · " + status;
            return reason == null ? summary : summary + " · " + reason;
        }

        public void SetStatus(Dictionary<string, object> snapshot)
        {
            if (snapshot == null || connection != "connected") return;
            string nextInstallation = Reported(snapshot, "installation_id");
            string nextInstance = Reported(snapshot, "runtime_instance");
            if (nextInstallation == null || nextInstance == null)
                continuity = "实例信息不完整";
            else if (hasSample && installation != null && installation != nextInstallation)
                continuity = "已切换服务器安装";
            else if (hasSample && installation == nextInstallation && instance != null && instance != nextInstance)
                continuity = "后台已重启";
            else if (installation == null || instance == null)
                continuity = null;
            installation = nextInstallation;
            instance = nextInstance;
            hasSample = fresh = true;

            var progress = Get(snapshot, "progress") as Dictionary<string, object>;
            var platforms = Get(snapshot, "platforms") as Dictionary<string, object>;
            rows.SuspendLayout();
            try
            {
                RenderSampleState();
                values["GeneratedAt"].Text = SampleTime(snapshot);
                values["InstallationId"].Text = installation ?? Missing;
                values["RuntimeInstance"].Text = instance ?? Missing;
                values["ServerStatus"].Text = State(Reported(snapshot, "status"));
                values["HeartbeatCount"].Text = Count(snapshot, "heartbeat_count");
                ShowSyntheticDialogue(Get(snapshot, "synthetic_dialogue") as Dictionary<string, object>);
                values["Queued"].Text = Count(progress, "queued");
                values["Running"].Text = Count(progress, "running");
                string completed = Count(progress, "completed");
                values["Completed"].Text = completed;
                values["Failed"].Text = Count(progress, "failed");
                string last = Reported(progress, "last_completed_id");
                bool none = completed == "0" && progress != null &&
                    progress.ContainsKey("last_completed_id") && progress["last_completed_id"] == null;
                values["LastCompletedId"].Text = last ?? (none ? "暂无完成记录" : Missing);
                values["PlatformLocal"].Text = Platform(platforms, "local");
                values["PlatformQq"].Text = Platform(platforms, "qq");
                values["PlatformWechat"].Text = Platform(platforms, "wechat");
                values["PlatformBilibili"].Text = Platform(platforms, "bilibili");
            }
            finally { rows.ResumeLayout(true); }
        }

        protected override void Dispose(bool disposing)
        {
            base.Dispose(disposing);
            if (disposing && headingFont != null) headingFont.Dispose();
        }
    }
}
