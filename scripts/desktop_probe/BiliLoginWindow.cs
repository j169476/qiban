// SPDX-License-Identifier: MIT
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Qiban.DesktopProbe;

// Passwords, SMS and CAPTCHA stay on Bilibili's own page. The host receives only
// the resulting session cookies from a fresh private profile, never page messages.
internal sealed class BiliLoginWindow : Form
{
    readonly WebView2 browser=new(){Dock=DockStyle.Fill};
    const string Passport="https://passport.bilibili.com/login";
    readonly Label note=new(){Dock=DockStyle.Fill,TextAlign=ContentAlignment.MiddleLeft,Text="正在打开 B 站登录页…"};
    readonly Button reload=new(){Text="刷新登录页",Dock=DockStyle.Right,AutoSize=true,Enabled=false};
    readonly System.Windows.Forms.Timer poll=new(){Interval=800};
    readonly TaskCompletionSource<Dictionary<string,string>?> completion=new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly System.Diagnostics.Stopwatch elapsed=System.Diagnostics.Stopwatch.StartNew();
    Dictionary<string,string>? result;
    bool checking, finishing, verificationActive, verificationUsesAlternate;
    TimeSpan? verificationStarted;
    readonly HashSet<Form> popups=[];
    static bool InDomain(Uri uri,string domain) => uri.Host==domain || uri.Host.EndsWith("."+domain,StringComparison.OrdinalIgnoreCase);
    static bool Allowed(string address,bool verification) => Uri.TryCreate(address,UriKind.Absolute,out var uri) &&
        uri.Scheme=="https" && uri.Port==443 && uri.UserInfo=="" && (InDomain(uri,"bilibili.com") ||
        (verification && (InDomain(uri,"geetest.com") || InDomain(uri,"geevisit.com"))));
    internal BiliLoginWindow()
    {
        Text="B站官方登录 · 手机验证码 / 账号密码";ClientSize=new(940,700);MinimumSize=new(680,520);
        StartPosition=FormStartPosition.CenterParent;
        var bar=new Panel{Dock=DockStyle.Top,Height=46,Padding=new Padding(8,4,8,4)};
        bar.Controls.Add(note);bar.Controls.Add(reload);Controls.Add(browser);Controls.Add(bar);
        reload.Click+=(_,_)=>{
            if(browser.CoreWebView2 is null || finishing)return;
            foreach(var popup in popups.ToArray())popup.Close();
            verificationStarted=null;verificationActive=false;verificationUsesAlternate=false;
            note.Text="正在重新打开 B 站登录页…";browser.CoreWebView2.Navigate(Passport);
        };
        browser.Resize+=(_,_)=>FitLoginPage();
        Shown+=async(_,_)=>await Initialize();
        poll.Tick+=async(_,_)=>await Check();
        FormClosing+=async(_,e)=>
        {
            if(finishing)return;
            e.Cancel=true;finishing=true;poll.Stop();
            foreach(var popup in popups.ToArray())popup.Close();
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
            var area=Screen.FromControl(Owner??this).WorkingArea;
            Size=new Size(Math.Min(area.Width,LogicalToDeviceUnits(1140)),Math.Min(area.Height,LogicalToDeviceUnits(790)));
            if(Owner is not null)CenterToParent();
            var environment=await CoreWebView2Environment.CreateAsync(userDataFolder:Program.Paths.WebView);
            var options=environment.CreateCoreWebView2ControllerOptions();
            options.ProfileName="bili-"+Guid.NewGuid().ToString("N");options.IsInPrivateModeEnabled=true;
            await browser.EnsureCoreWebView2Async(environment,options);
            if(finishing || IsDisposed)return;
            var core=browser.CoreWebView2;
            Configure(core,environment,options,false);
            core.NavigationCompleted+=(_,e)=>{
                if(finishing || e.WebErrorStatus==CoreWebView2WebErrorStatus.OperationCanceled || verificationActive)return;
                note.Text=e.IsSuccess?"请在 B 站页面完成登录。":"登录页加载失败，请刷新重试。";
            };
            // These are the public verification requests made by both official login methods.
            // Report progress without accessing form values, request bodies or challenge tokens.
            foreach(var domain in new[]{"geetest.com","geevisit.com"})
                core.AddWebResourceRequestedFilter("https://*."+domain+"/*",CoreWebView2WebResourceContext.All);
            core.WebResourceRequested+=(_,e)=>{
                if(!Uri.TryCreate(e.Request.Uri,UriKind.Absolute,out var u))return;
                // Some versions of the widget select their backup CDN for scripts and images
                // but still refer to the unavailable primary CDN for the layout stylesheet.
                if(verificationUsesAlternate && e.ResourceContext==CoreWebView2WebResourceContext.Stylesheet &&
                   e.Request.Method=="GET" && u.Scheme=="https" && u.Port==443 && u.Host=="static.geetest.com" &&
                   u.UserInfo=="" && u.Query=="" && u.AbsolutePath.StartsWith("/static/",StringComparison.Ordinal) &&
                   u.AbsolutePath.EndsWith(".css",StringComparison.OrdinalIgnoreCase)){
                    var alternate=new UriBuilder(u){Host="static.geevisit.com"}.Uri.AbsoluteUri;
                    e.Response=environment.CreateWebResourceResponse(null,307,"Temporary Redirect",
                        "Location: "+alternate+"\r\nCache-Control: no-store");
                    return;
                }
                if(u.AbsolutePath=="/gettype.php" && (InDomain(u,"geetest.com") || InDomain(u,"geevisit.com"))){
                    verificationActive=true;verificationUsesAlternate=false;
                    verificationStarted=elapsed.Elapsed;note.Text="正在加载 B 站安全验证…";
                }
            };
            core.WebResourceResponseReceived+=(_,e)=>{
                if(finishing || !Uri.TryCreate(e.Request.Uri,UriKind.Absolute,out var u) ||
                        !(InDomain(u,"geetest.com") || InDomain(u,"geevisit.com")))return;
                if(u.Host=="static.geevisit.com" && u.AbsolutePath.StartsWith("/static/js/",StringComparison.Ordinal) &&
                    e.Response.StatusCode is >=200 and <300)verificationUsesAlternate=true;
                if(!verificationActive || u.Host=="monitor.geetest.com")return;
                if(e.Response.StatusCode==0 || e.Response.StatusCode>=400){
                    verificationStarted=null;note.Text="安全验证加载失败，可以刷新登录页重试。";
                }else if(u.AbsolutePath.EndsWith(".css",StringComparison.OrdinalIgnoreCase) &&
                        e.Response.StatusCode is >=200 and <300){
                    verificationStarted=null;note.Text="请在 B 站页面完成安全验证。";
                }
            };
            FitLoginPage();reload.Enabled=true;core.Navigate(Passport);poll.Start();
        }
        catch(Exception error) when(error is InvalidOperationException or System.Runtime.InteropServices.COMException or IOException)
        {note.Text="官方登录窗口未能打开，请关闭后重试。";}
    }
    void FitLoginPage()
    {
        if(browser.CoreWebView2 is null || browser.ClientSize.Width<=0)return;
        // The official desktop form has a minimum layout width of roughly 1100 CSS pixels.
        browser.ZoomFactor=Math.Clamp(browser.ClientSize.Width/(double)LogicalToDeviceUnits(1120),0.5,1.0);
    }
    void Configure(CoreWebView2 core,CoreWebView2Environment environment,
        CoreWebView2ControllerOptions options,bool verification)
    {
        core.Settings.AreDevToolsEnabled=false;core.Settings.AreHostObjectsAllowed=false;
        core.Settings.IsWebMessageEnabled=false;core.Settings.IsPasswordAutosaveEnabled=false;
        core.Settings.IsGeneralAutofillEnabled=false;
        core.NavigationStarting+=(_,e)=>{
            if(Allowed(e.Uri,verification) || (verification && e.Uri=="about:blank"))return;
            e.Cancel=true;note.Text="该链接无法在登录窗口打开。";
        };
        core.NewWindowRequested+=async(_,e)=>await OpenVerification(e,environment,options);
        core.PermissionRequested+=(_,e)=>e.State=CoreWebView2PermissionState.Deny;
        core.DownloadStarting+=(_,e)=>e.Cancel=true;
    }
    async Task OpenVerification(CoreWebView2NewWindowRequestedEventArgs request,
        CoreWebView2Environment environment,CoreWebView2ControllerOptions options)
    {
        request.Handled=true;
        if(finishing)return;
        if(request.Uri!="about:blank" && !Allowed(request.Uri,true)){
            note.Text="该链接无法在登录窗口打开。";return;
        }
        if(popups.Count>=2){note.Text="请先完成已打开的验证窗口。";return;}
        using var deferral=request.GetDeferral();
        var popup=new Form{Text="B 站登录验证",StartPosition=FormStartPosition.CenterParent,ClientSize=browser.ClientSize};
        var child=new WebView2{Dock=DockStyle.Fill};
        popup.Controls.Add(child);popups.Add(popup);
        popup.FormClosed+=(_,_)=>popups.Remove(popup);
        try{
            popup.Show(this);
            await child.EnsureCoreWebView2Async(environment,options);
            if(finishing || popup.IsDisposed)return;
            var core=child.CoreWebView2;
            Configure(core,environment,options,true);
            core.WindowCloseRequested+=(_,_)=>popup.Close();
            request.NewWindow=core;
        }
        catch(Exception error) when(error is InvalidOperationException or System.Runtime.InteropServices.COMException){
            if(!popup.IsDisposed)popup.Close();
            if(!finishing)note.Text="验证窗口未能打开，请刷新登录页重试。";
        }
    }
    async Task Check()
    {
        if(checking || finishing || browser.CoreWebView2 is null)return;
        if(elapsed.Elapsed>TimeSpan.FromMinutes(5)){Close();return;}
        if(verificationStarted is TimeSpan start && elapsed.Elapsed-start>TimeSpan.FromSeconds(12))
            note.Text="安全验证加载较慢，可以继续等待或刷新登录页。";
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
            if(found.ContainsKey("sessdata") && found.ContainsKey("bili_jct"))
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
