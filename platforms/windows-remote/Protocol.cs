using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace Qiban.Remote
{
    internal sealed class LinkFailure : Exception
    {
        internal readonly string Code;
        internal bool CanRetry { get { return Code == "disconnected"; } }
        internal LinkFailure(string code, string message) : base(message) { Code = code; }
    }

    internal sealed partial class StatusLink
    {
        private string installation, instance;
        private static LinkFailure BadReply() { return new LinkFailure("protocol", "服务器返回了不符合 v1 协议的数据。"); }

        internal static Dictionary<string,object> Map(object value)
        {
            var result = value as Dictionary<string,object>;
            if (result == null) throw BadReply();
            return result;
        }

        private static object Field(Dictionary<string,object> map, string key)
        {
            object value;
            if (!map.TryGetValue(key, out value)) throw BadReply();
            return value;
        }

        internal static long Number(Dictionary<string,object> map, string key)
        {
            object value = Field(map, key);
            if (!(value is int || value is long) || Convert.ToInt64(value) < 0) throw BadReply();
            return Convert.ToInt64(value);
        }

        private static string Id(Dictionary<string,object> map, string key)
        {
            var id = Field(map, key) as string;
            if (id == null || !Regex.IsMatch(id, @"\A[a-f0-9]{32}\z")) throw BadReply();
            return id;
        }

        // A partial snapshot may omit display metrics. Validate each supplied value before rendering.
        private static void ValidateDisplayFields(Dictionary<string,object> result)
        {
            OptionalType(result, "status", typeof(string));
            if (result.ContainsKey("heartbeat_count")) Number(result, "heartbeat_count");
            if (result.ContainsKey("progress")) {
                var progress = Map(result["progress"]);
                foreach (var key in new[] {"queued","running","completed","failed"})
                    if (progress.ContainsKey(key)) Number(progress, key);
                if (progress.ContainsKey("last_completed_id") && progress["last_completed_id"] != null)
                    OptionalType(progress, "last_completed_id", typeof(string));
            }
            object dialogueValue;
            if (result.TryGetValue("synthetic_dialogue", out dialogueValue) && dialogueValue != null) {
                var dialogue = Map(dialogueValue);
                object backend, mode;
                if (dialogue.TryGetValue("backend", out backend) && Object.Equals(backend, "postgresql") &&
                    dialogue.TryGetValue("mode", out mode) && Object.Equals(mode, "synthetic")) {
                    foreach (var name in new[] {"confirmed","uncertain","submissions","denied","replays"})
                        if (dialogue.ContainsKey(name)) Number(dialogue, name);
                }
            }
            if (result.ContainsKey("platforms")) {
                var platforms = Map(result["platforms"]);
                foreach (var name in new[] {"local","qq","wechat","bilibili"}) {
                    if (!platforms.ContainsKey(name)) continue;
                    var item = Map(platforms[name]);
                    OptionalType(item, "available", typeof(bool));
                    OptionalType(item, "status", typeof(string));
                    OptionalType(item, "reason", typeof(string));
                }
            }
        }

        private static void OptionalType(Dictionary<string,object> map, string key, Type type)
        {
            object value;
            if (map.TryGetValue(key,out value) && !type.IsInstanceOfType(value))
                throw new LinkFailure("protocol", "服务器状态字段类型无效：" + key);
        }

        internal async System.Threading.Tasks.Task Negotiate()
        {
            var hello = await Request("protocol.hello", new { versions = new[] {1} }).ConfigureAwait(false);
            var ops = Field(hello, "operations") as IList;
            if (Number(hello, "protocol_version") != 1 || ops == null || !ops.Cast<object>().Contains("runtime.status"))
                throw new LinkFailure("protocol_incompatible", "双方没有共同支持的状态读取协议。");
            installation = Id(hello, "installation_id");
            instance = Id(hello, "runtime_instance");
        }

        internal async System.Threading.Tasks.Task<Dictionary<string,object>> Snapshot()
        {
            var result = await Request("runtime.status", new { }).ConfigureAwait(false);
            if (Number(result, "protocol_version") != 1) throw BadReply();
            if (Id(result, "installation_id") != installation || Id(result, "runtime_instance") != instance)
                throw new LinkFailure("disconnected", "服务实例发生变化。");
            string generated = Field(result, "generated_at") as string;
            DateTimeOffset time;
            if (generated == null || !generated.EndsWith("Z", StringComparison.Ordinal) ||
                !DateTimeOffset.TryParse(generated, CultureInfo.InvariantCulture, DateTimeStyles.None, out time))
                throw BadReply();
            ValidateDisplayFields(result);
            return result;
        }
    }
}
