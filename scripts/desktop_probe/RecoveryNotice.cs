// SPDX-License-Identifier: MIT
namespace Qiban.DesktopProbe;

// A completed recovery is transient; pending or failed checks must remain visible.
// The monotonic clock is provided by the window, so wall-clock changes cannot hide it.
internal sealed class RecoveryNotice
{
    long? deadline;
    internal void Checking() => deadline=null;
    internal void Failed() => deadline=null;
    internal void Succeeded(long now) => deadline=now+8000;
    internal bool ShouldDismiss(long now, bool ready)
    {
        if(!ready || deadline is null || now<deadline.Value)return false;
        deadline=null;
        return true;
    }
}
