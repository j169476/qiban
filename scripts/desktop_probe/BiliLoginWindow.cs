// SPDX-License-Identifier: MIT
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Qiban.DesktopProbe;

// Passwords, SMS and CAPTCHA stay on Bilibili's own page. The host receives only
// the resulting session cookies from a fresh private profile, never page messages.
internal sealed class BiliLoginWindow : Form
{
    readonly WebView2 browser=new(){Dock=DockStyle.Fill};
    readonly Label note=new(){Dock=DockStyle.Top,Height=48,Text="请在 B 站官方页面切换密码或手机验证码登录。登录成功后自动返回栖伴。"};
    readonly System.Windows.Forms.Timer poll=new(){Interval=800};
    readonly TaskCompletionSource<Dictionary<string,string>?> completion=new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly System.Diagnostics.Stopwatch elapsed=System.Diagnostics.Stopwatch.StartNew();
    Dictionary<string,string>? result;
    bool checking, finishing;
    static bool Allowed(string uri) => Uri.TryCreate(uri,UriKind.Absolute,out var u) &&
        u.Scheme=="https" && u.Port==443 && u.UserInfo=="" &&
        (u.Host=="bilibili.com" || u.Host.EndsWith(".bilibili.com",StringComparison.OrdinalIgnoreCase));
    internal BiliLoginWindow()
    {
        Text="B站官方登录 · 手机验证码 / 账号密码";ClientSize=new(940,700);MinimumSize=new(680,520);
        StartPosition=FormStartPosition.CenterParent;Controls.Add(browser);Controls.Add(note);
        Shown+=async(_,_)=>await Initialize();
        poll.Tick+=async(_,_)=>await Check();
        FormClosing+=async(_,e)=>
        {
            if(finishing)return;
            e.Cancel=true;finishing=true;poll.Stop();
            try {if(browser.CoreWebView2 is not null)await browser.CoreWebView2.Profile.ClearBrowsingDataAsync();}
            catch(Exception error) when(error is InvalidOperationException or System.Runtime.InteropServices.COMException){}
            Close();
        };
        FormClosed+=(_,_)=>completion.TrySetResult(result);
    }
    async Task Initialize()
    {
        try
        {
            var environment=await CoreWebView2Environment.CreateAsync(userDataFolder:Program.Paths.WebView);
            var options=environment.CreateCoreWebView2ControllerOptions();
            options.ProfileName="bili-"+Guid.NewGuid().ToString("N");options.IsInPrivateModeEnabled=true;
            await browser.EnsureCoreWebView2Async(environment,options);
            if(finishing || IsDisposed)return;
            var core=browser.CoreWebView2;
            core.Settings.AreDevToolsEnabled=false;core.Settings.AreHostObjectsAllowed=false;
            core.Settings.IsWebMessageEnabled=false;core.Settings.IsPasswordAutosaveEnabled=false;
            core.Settings.IsGeneralAutofillEnabled=false;
            core.NavigationStarting+=(_,e)=>{if(!Allowed(e.Uri))e.Cancel=true;};
            core.NewWindowRequested+=(_,e)=>{e.Handled=true;if(Allowed(e.Uri))core.Navigate(e.Uri);};
            core.PermissionRequested+=(_,e)=>e.State=CoreWebView2PermissionState.Deny;
            core.DownloadStarting+=(_,e)=>e.Cancel=true;
            core.Navigate("https://passport.bilibili.com/login");poll.Start();
        }
        catch(Exception error) when(error is InvalidOperationException or System.Runtime.InteropServices.COMException or IOException)
        {note.Text="官方登录窗口未能打开，请关闭后重试。";}
    }
    async Task Check()
    {
        if(checking || finishing || browser.CoreWebView2 is null)return;
        if(elapsed.Elapsed>TimeSpan.FromMinutes(5)){Close();return;}
        checking=true;
        try
        {
            var cookies=await browser.CoreWebView2.CookieManager.GetCookiesAsync("https://www.bilibili.com");
            if(finishing || IsDisposed)return;
            var names=new Dictionary<string,string>{{"SESSDATA","sessdata"},{"bili_jct","bili_jct"},
                {"buvid3","buvid3"},{"buvid4","buvid4"},{"DedeUserID","dedeuserid"}};
            var found=new Dictionary<string,string>();
            foreach(var cookie in cookies)
                if(names.TryGetValue(cookie.Name,out var name) && !string.IsNullOrEmpty(cookie.Value) && cookie.Value.Length<=8192)
                    found[name]=cookie.Value;
            if(found.ContainsKey("sessdata") && found.ContainsKey("bili_jct") && found.ContainsKey("dedeuserid"))
            {result=found;Close();}
        }
        catch(Exception error) when(error is InvalidOperationException or System.Runtime.InteropServices.COMException)
        {note.Text="登录状态暂未返回，请完成验证，或关闭后重试。";}
        finally{checking=false;}
    }
    internal async Task<Dictionary<string,string>?> Login(Form owner)
    {Show(owner);return await completion.Task;}
    protected override void Dispose(bool disposing)
    {if(disposing){poll.Stop();poll.Dispose();browser.Dispose();completion.TrySetResult(null);}base.Dispose(disposing);}
}
