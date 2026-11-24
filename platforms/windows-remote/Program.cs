using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Qiban.Remote
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var options = new Dictionary<string,string>(StringComparer.Ordinal);
            try {
                string[] names = {"--profile","--host","--port","--identity","--known-hosts","--verify","--wrong-hosts"};
                for (int i = 0; i < args.Length; i += 2) {
                    if (i + 1 == args.Length || !names.Contains(args[i])) throw new ArgumentException("无效启动参数。");
                    options.Add(args[i].Substring(2), args[i+1]);
                }
                using (var window = new RemoteWindow(options)) {
                    int result = 0;
                    if (options.ContainsKey("verify")) {
                        window.Shown += async (sender,e) => {
                            result = await Acceptance.Run(window, options);
                            window.Close();
                        };
                    }
                    Application.Run(window);
                    return result;
                }
            } catch (Exception e) {
                MessageBox.Show(e.Message, "栖伴远程控制台", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
        }
    }
}
