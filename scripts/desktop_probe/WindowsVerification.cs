// SPDX-License-Identifier: MIT
using System.Runtime.InteropServices;
using Windows.Security.Credentials.UI;

namespace Qiban.DesktopProbe;

internal static class WindowsVerification
{
    internal static async Task<string> Availability()
    {
        if(!OperatingSystem.IsWindowsVersionAtLeast(10,0,22000))return "UnsupportedWindows";
        try{return (await UserConsentVerifier.CheckAvailabilityAsync()).ToString();}
        catch(COMException){return "Unavailable";}
    }

    internal static async Task<string> Verify(IntPtr window,CancellationToken token)
        =>await Verify(window,token,"验证当前 Windows 身份，以重设栖伴应用密码。现有资料会保留。");

    internal static Task<string> VerifyRestore(IntPtr window,CancellationToken token)
        =>Verify(window,token,"验证当前 Windows 身份，以从所选备份覆盖恢复栖伴资料。当前应用密码锁保持不变。");

    // UPGRADE-177: one confirmation covers one whole rollback task - the maintenance steps behind
    // it never prompt again - so the sentence names the task and what the person is agreeing to.
    // The ordinary way back keeps this machine's newest settings; only the emergency branch returns
    // to the older generation's own settings, and the two prompts must say so.
    internal static Task<string> VerifyUpgrade(IntPtr window,CancellationToken token,bool escalated=false)
        =>Verify(window,token,escalated
            ?"验证当前 Windows 身份，以紧急回退栖伴：升级后的内容会先封存，应用设置回到升级前的状态。"
            :"验证当前 Windows 身份，以把栖伴退回到升级前的程序与资料。升级后的资料会先保全，本机设置保持不变。");

    static async Task<string> Verify(IntPtr window,CancellationToken token,string message)
    {
        if(!OperatingSystem.IsWindowsVersionAtLeast(10,0,22000))return "UnsupportedWindows";
        try
        {
            string available=await Availability();
            if(available!="Available")return available;
            return (await UserConsentVerifierInterop.RequestVerificationForWindowAsync(window,
                message).AsTask(token)).ToString();
        }
        catch(COMException){return "Unavailable";}
    }

    internal static string Message(string result)=>result switch
    {
        "Canceled"=>"已取消 Windows 验证，应用密码未改变。",
        "DeviceBusy"=>"Windows 验证设备正忙，请稍后再试。",
        "NotConfiguredForUser"=>"请先在 Windows 登录选项中设置 PIN、人脸或指纹，再重设应用密码。",
        "DisabledByPolicy"=>"Windows 策略不允许身份验证，应用密码未改变。",
        "RetriesExhausted"=>"Windows 验证尝试次数已用尽，请稍后再试。",
        "UnsupportedWindows"=>"当前系统不支持此桌面验证入口；应用密码未改变。",
        _=>"Windows 身份验证未完成，应用密码未改变。"
    };

    internal static string RestoreMessage(string result)=>result switch
    {
        "Canceled"=>"已取消 Windows 验证，当前资料未改变。请重新查看预览后再恢复。",
        "NotConfiguredForUser"=>"请先在 Windows 登录选项中设置 PIN、人脸或指纹，才能覆盖恢复资料。",
        "DeviceBusy"=>"Windows 验证设备正忙，资料未改变，请稍后重试。",
        _=>"Windows 身份验证未完成，当前资料未改变。请检查系统验证设置后重试。"
    };

    // Every unfinished state of this confirmation says that nothing was executed, so a person never
    // has to guess whether a cancelled prompt already started a rollback.
    internal static string UpgradeMessage(string result)=>result switch
    {
        "Canceled"=>"已取消 Windows 验证，没有执行回退，程序与资料都未改变。",
        "NotConfiguredForUser"=>"请先在 Windows 登录选项中设置 PIN、人脸或指纹，才能执行回退。",
        "DeviceBusy"=>"Windows 验证设备正忙，没有执行回退，请稍后再试。",
        "DisabledByPolicy"=>"Windows 策略不允许身份验证，没有执行回退。",
        "RetriesExhausted"=>"Windows 验证尝试次数已用尽，没有执行回退，请稍后再试。",
        "UnsupportedWindows"=>"当前系统不支持此桌面验证入口，没有执行回退。",
        _=>"Windows 身份验证未完成，没有执行回退，程序与资料都未改变。"
    };
}
