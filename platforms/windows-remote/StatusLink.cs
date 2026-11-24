using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace Qiban.Remote
{
    internal sealed partial class StatusLink : IDisposable
    {
        private const int MaxLine = 65536;
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false,true);
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength=MaxLine, RecursionLimit=20 };
        private readonly Process ssh;
        private readonly Stream input, output;
        private readonly byte[] buffer = new byte[4096];
        private int offset, count;
        private volatile string terminalError;
        private volatile bool disposed;

        internal StatusLink(ConnectionSettings settings)
        {
            var info = new ProcessStartInfo(ConnectionSettings.SshExecutable(), settings.Arguments()) {
                UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true,
                RedirectStandardOutput=true, RedirectStandardError=true
            };
            info.EnvironmentVariables["LC_ALL"] = "C";
            ssh = new Process {StartInfo=info};
            ssh.ErrorDataReceived += (sender, e) => Classify(e.Data);
            try {
                ssh.Start();
                ssh.BeginErrorReadLine();
                input = ssh.StandardInput.BaseStream; output = ssh.StandardOutput.BaseStream;
            } catch { ssh.Dispose(); throw; }
        }

        private void Classify(string text)
        {
            if (text == null) return;
            string s = text.ToLowerInvariant();
            if (s.Contains("host key verification failed") || s.Contains("host identification has changed") ||
                (s.Contains("host key is known") && s.Contains("strict checking"))) terminalError = "host_key";
            else if (terminalError == null && (s.Contains("permission denied") || s.Contains("load key ") ||
                s.Contains("bad permissions") || s.Contains("signing failed"))) terminalError = "authentication";
            else if (terminalError == null && (s.Contains("bad configuration") || s.Contains("unsupported option") ||
                s.Contains("bad owner") || s.Contains("no matching "))) terminalError = "ssh_configuration";
        }

        private LinkFailure Closed()
        {
            if (!disposed && ssh.WaitForExit(800)) ssh.WaitForExit(); // Drain redirected diagnostics before classifying.
            switch (terminalError) {
                case "host_key": return new LinkFailure("host_key", "服务器公钥与信任文件不匹配或尚未登记。请通过服务器控制台核对。");
                case "authentication": return new LinkFailure("authentication", "客户端公钥认证失败。请检查登记的密钥和文件权限。");
                case "ssh_configuration": return new LinkFailure("ssh_configuration", "SSH 配置或密钥算法不兼容。");
                default: return new LinkFailure("disconnected", "SSH 已断开或服务暂不可达。");
            }
        }

        private async Task<Dictionary<string,object>> Request(string operation, object arguments)
        {
            string id = Guid.NewGuid().ToString("N");
            var bytes = Utf8.GetBytes(json.Serialize(new { action="management", operation=operation,
                arguments=arguments, request_id=id }) + "\n");
            var exchange = Exchange(bytes);
            if (await Task.WhenAny(exchange, Task.Delay(12000)).ConfigureAwait(false) != exchange) {
                ObserveAbandoned(exchange);
                throw new LinkFailure("disconnected", "服务器未在时限内响应。");
            }
            string line = await exchange.ConfigureAwait(false);
            Dictionary<string,object> reply;
            try { reply = Map(json.DeserializeObject(line)); }
            catch (ArgumentException) { throw BadReply(); }
            catch (InvalidOperationException) { throw BadReply(); }
            if (!Object.Equals(Field(reply, "request_id"), id) || !(Field(reply,"ok") is bool)) throw BadReply();
            if (!(bool)reply["ok"]) {
                bool incompatible = Object.Equals(Field(reply, "error"), "protocol_incompatible");
                throw new LinkFailure(incompatible ? "protocol_incompatible" : "operation_refused",
                    incompatible ? "双方没有共同支持的协议版本。" : "服务器拒绝了只读状态请求。");
            }
            return Map(Field(reply, "result"));
        }

        private static async void ObserveAbandoned(Task<string> exchange)
        {
            try { await exchange.ConfigureAwait(false); } catch (Exception) { /* Session has been discarded. */ }
        }

        private async Task<string> Exchange(byte[] request)
        {
            try {
                await input.WriteAsync(request,0,request.Length).ConfigureAwait(false);
                await input.FlushAsync().ConfigureAwait(false);
                using (var line = new MemoryStream()) {
                    while (true) {
                        if (offset == count) {
                            count = await output.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false);
                            offset = 0;
                            if (count == 0) throw Closed();
                        }
                        int end = Array.IndexOf(buffer,(byte)10,offset,count-offset);
                        int length = (end < 0 ? count : end) - offset;
                        if (line.Length + length >= MaxLine) throw BadReply();
                        line.Write(buffer,offset,length); offset += length;
                        if (end >= 0) {
                            offset++;
                            try { return Utf8.GetString(line.GetBuffer(),0,(int)line.Length); }
                            catch (DecoderFallbackException) { throw BadReply(); }
                        }
                    }
                }
            } catch (IOException) { throw Closed(); }
        }

        internal void Interrupt() { if (!ssh.HasExited) ssh.Kill(); }

        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            try { Interrupt(); } catch (InvalidOperationException) { }
            ssh.Dispose();
        }
    }
}
