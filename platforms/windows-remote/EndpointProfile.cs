using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

namespace Qiban.Remote
{
    internal static class EndpointProfile
    {
        internal static ConnectionSettings Load(string filename)
        {
            string path = Path.GetFullPath(filename);
            if (path.StartsWith(@"\\")) throw new ArgumentException("请选择本机连接文件。");
            Dictionary<string,object> profile;
            using (var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                if (file.Length > 65536) throw new ArgumentException("连接文件超过 64 KiB。");
                using (var reader = new StreamReader(file, new UTF8Encoding(false,true), true)) {
                    profile = new JavaScriptSerializer {MaxJsonLength=65536, RecursionLimit=8}
                        .DeserializeObject(reader.ReadToEnd()) as Dictionary<string,object>;
                }
            }
            if (profile == null) throw new ArgumentException("无效的连接文件。");
            Func<string,string> field = key => {
                object value;
                if (!profile.TryGetValue(key,out value) || !(value is string) || String.IsNullOrWhiteSpace((string)value))
                    throw new ArgumentException("连接文件的 " + key + " 字段无效。");
                return (string)value;
            };
            object port;
            if (field("user") != "qiban-status" || !profile.TryGetValue("port",out port) || !(port is int))
                throw new ArgumentException("连接文件须包含 qiban-status 账号和有效的数字端口。");
            string folder = Path.GetDirectoryName(path);
            return new ConnectionSettings(field("host"), (int)port, Path.Combine(folder,field("identity_file")),
                Path.Combine(folder,field("known_hosts")));
        }
    }
}
