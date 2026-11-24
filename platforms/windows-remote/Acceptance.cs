using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace Qiban.Remote
{
    internal static class Acceptance
    {
        private static string ReadLabel(RemoteWindow window, string name)
        {
            var controls = window.View.Controls.Find(name, true);
            if (controls.Length != 1) throw new InvalidDataException("缺少状态控件 " + name);
            return controls[0].Text;
        }

        private static async Task Until(Func<bool> ready, string condition, int seconds = 35)
        {
            var timer = Stopwatch.StartNew();
            while (!ready()) {
                if (timer.Elapsed.TotalSeconds > seconds) throw new TimeoutException(condition);
                await Task.Delay(100);
            }
        }

        private static void Require(bool condition, string description)
        {
            if (!condition) throw new InvalidDataException(description);
        }

        internal static async Task<int> Run(RemoteWindow window, Dictionary<string,string> options)
        {
            var evidence = new Dictionary<string,object>();
            string path = Path.GetFullPath(options["verify"]);
            string stage = "setup";
            var json = new JavaScriptSerializer();
            try {
                Directory.CreateDirectory(path);
                if (File.Exists(Path.Combine(path, "resume.txt")) || File.Exists(Path.Combine(path, "offline.json")))
                    throw new IOException("需使用新的验收目录。");
                stage = "first_connection";
                window.ConnectForCheck();
                await Until(() => window.LastSample != null || window.State == "error", stage);
                Require(window.State == "connected", stage + ": " + window.FailureCode);
                var first = window.LastSample;
                evidence["before"] = first;
                Require(ReadLabel(window,"RuntimeInstance").Contains((string)first["runtime_instance"]), "首个实例未显示");
                var progress = StatusLink.Map(first["progress"]);
                long before = StatusLink.Number(progress, "completed");
                string oldSampleState = ReadLabel(window, "SampleState");
                stage = "manual_disconnect";
                window.DisconnectForCheck();
                Require(window.State == "disconnected", stage);
                string stale = ReadLabel(window, "SampleState");
                Require(stale != oldSampleState && stale.Contains("离线"), "未标记离线样本");
                Require(ReadLabel(window,"RuntimeInstance").Contains((string)first["runtime_instance"]), "离线样本丢失");
                evidence["offline_label"] = stale;
                File.WriteAllText(Path.Combine(path, "offline.json"), json.Serialize(first));

                stage = "offline_task";
                string resume = Path.Combine(path, "resume.txt");
                await Until(() => File.Exists(resume), stage, 180);
                string job = File.ReadAllText(resume).Trim();
                Require(job.Length > 0 && window.State == "disconnected" && window.SuccessfulConnections == 1, "未保持手动断开");
                stage = "reconnect";
                window.ConnectForCheck();
                await Until(() => window.SuccessfulConnections == 2 || window.State == "error", stage);
                var after = window.LastSample;
                Require(window.State == "connected" && Object.Equals(after["installation_id"], first["installation_id"]) &&
                    Object.Equals(after["runtime_instance"], first["runtime_instance"]), "重连到不同安装或服务实例");
                var current = StatusLink.Map(after["progress"]);
                Require(StatusLink.Number(current,"completed") > before && Object.Equals(current["last_completed_id"],job) &&
                    StatusLink.Number(after,"heartbeat_count") > StatusLink.Number(first,"heartbeat_count"), "后台离线任务进度未获得证明");
                Require(ReadLabel(window,"LastCompletedId").Contains(job) &&
                    ReadLabel(window,"Completed").Contains(current["completed"].ToString()), "任务进度未显示");
                evidence["after"] = after;
                stage = "automatic_reconnect";
                window.InterruptForCheck();
                await Until(() => window.State == "reconnecting", stage);
                await Until(() => window.SuccessfulConnections == 3 && window.State == "connected", stage);
                Require(Object.Equals(window.LastSample["runtime_instance"],first["runtime_instance"]), "自动重连实例不符");
                evidence["automatic_reconnect"] = true;

                stage = "wrong_host_key";
                window.DisconnectForCheck();
                window.SetTrustForCheck(options["wrong-hosts"]);
                window.ConnectForCheck();
                await Until(() => window.State == "error", stage);
                Require(window.FailureCode == "host_key" && window.SuccessfulConnections == 3, "主机密钥校验结果不符");
                evidence["wrong_key_result"] = window.FailureCode;
                evidence["wrong_key_display"] = ReadLabel(window, "ConnectionDetail");
                evidence["passed"] = true;
                return 0;
            } catch (Exception e) {
                evidence["passed"] = false; evidence["stage"] = stage; evidence["error"] = e.Message;
                evidence["connection_state"] = window.State; evidence["reason"] = window.FailureCode;
                return 1;
            } finally {
                File.WriteAllText(Path.Combine(path, "result.json"), json.Serialize(evidence));
            }
        }
    }
}
