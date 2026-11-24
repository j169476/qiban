using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;

namespace Qiban.Remote
{
    internal sealed class ConnectionSettings
    {
        internal readonly string Host, Identity, KnownHosts;
        internal readonly int Port;

        internal ConnectionSettings(string host, int port, string identity, string trust)
        {
            IPAddress address;
            host = host.Trim();
            if (host.Length == 0 || host.Length > 253 || (!IPAddress.TryParse(host, out address) &&
                !host.Split('.').All(label => Regex.IsMatch(label, @"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\z"))))
                throw new ArgumentException("请输入有效的主机名或 IP 地址。");
            if (port < 1 || port > 65535) throw new ArgumentException("端口应在 1 到 65535 之间。");
            Host = host; Port = port;
            Identity = LocalFile(identity, "客户端密钥");
            KnownHosts = LocalFile(trust, "已核对的服务器公钥文件");
        }

        private static string LocalFile(string value, string label)
        {
            if (String.IsNullOrWhiteSpace(value)) throw new ArgumentException("请选择" + label + "。");
            string path = Path.GetFullPath(value.Trim());
            if (path.StartsWith(@"\\") || path.Any(c => Char.IsControl(c) || "\"%$".IndexOf(c) >= 0) || !File.Exists(path))
                throw new ArgumentException(label + "必须是可用的本机文件。");
            return path;
        }

        internal static string SshExecutable()
        {
            string exe = Path.Combine(Environment.SystemDirectory, "OpenSSH", "ssh.exe");
            if (!File.Exists(exe)) throw new ArgumentException("请安装 Windows 可选功能 OpenSSH 客户端。");
            return exe;
        }

        internal string Arguments()
        {
            var args = new[] { "-F", "none", "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=4", "-o", "ServerAliveCountMax=2",
                "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=none", "-o", "UpdateHostKeys=no",
                "-o", "VerifyHostKeyDNS=no", "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
                "-o", "ClearAllForwardings=yes", "-o", "UserKnownHostsFile=\"" + KnownHosts.Replace('\\', '/') + "\"",
                "-i", Identity, "-p", Port.ToString(System.Globalization.CultureInfo.InvariantCulture),
                "-l", "qiban-status", "--", Host, "qiban-status" };
            return String.Join(" ", args.Select(QuoteArgument));
        }

        // ProcessStartInfo on .NET Framework accepts the Windows command-line form.
        private static string QuoteArgument(string value)
        {
            var text = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value) {
                if (c == '\\') { slashes++; continue; }
                text.Append('\\', c == '"' ? 2 * slashes + 1 : slashes).Append(c);
                slashes = 0;
            }
            return text.Append('\\', 2 * slashes).Append('"').ToString();
        }
    }
}
