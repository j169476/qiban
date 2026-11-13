// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// How one portable replacement ended. Only "Completed" and "Uncertain" may stop the old
// backend, and neither may offer a resubmission.
internal enum PortableSwitchOutcome { Completed, Uncertain, Refused, NotStarted }

// Cross-machine portable backup transfer (D54/D55).
//
// The window owns every file path: a page request carries only a recovery password or the
// preview token the backend just issued, never a path and never a switch. All backend
// traffic uses the service's one private envelope, {action:'desktop-portable',operation,
// arguments}, and replies are mapped field by field from src/qiban/desktop_portable.py —
// nothing else the service returns can reach the page.
internal static class DesktopPortable
{
    internal const string Action = "desktop-portable";
    internal const string ExportOperation = "export", PreviewOperation = "preview";
    internal const string BeginOperation = "begin", CompleteOperation = "complete", CancelOperation = "cancel";

    // One backend operation may use this much of the pipe budget. StorageComponent keeps
    // the worker reply read and the Call deadline matched to these bounds. A replacement
    // runs begin, the Windows verification, then complete, so its whole budget is all
    // three; the page deadline adds a margin on top.
    internal const int OperationSeconds = 240;
    internal const int CancelSeconds = 60;
    internal const int WindowsSeconds = 180;
    internal static TimeSpan RestoreTimeout => TimeSpan.FromSeconds(2 * OperationSeconds + WindowsSeconds);

    // The service and its worker answer these after the stager ran: the switch may already
    // be applied, so the outcome is unknown rather than refused — stop the old backend and
    // never resubmit. portable_switched is the worker's own refusal once the restart pointer
    // names a new generation; expired challenges and pre-commit refusals are not in this set.
    static readonly string[] UncertainSwitchCodes =
        { "portable_switch_uncertain", "portable_switch_applied", "portable_switched", "portable_outcome_unknown", "portable_failed" };

    internal delegate Task<JsonElement> Exchange(string operation, object arguments);

    internal static JsonElement Envelope(string operation, object arguments) =>
        JsonSerializer.SerializeToElement(new { action = Action, operation, arguments });

    // ------------------------------------------------------------------ requests

    internal static bool IsAction(JsonElement message, string action) => message.ValueKind == JsonValueKind.Object
        && message.TryGetProperty("action", out var value) && value.ValueKind == JsonValueKind.String && value.GetString() == action;

    internal static bool RequestId(JsonElement message, out string id)
    {
        id = "";
        if (!message.TryGetProperty("request_id", out var value) || value.ValueKind != JsonValueKind.String) return false;
        string? text = value.GetString();
        if (text is null || !System.Text.RegularExpressions.Regex.IsMatch(text, "\\A[a-f0-9]{32}\\z")) return false;
        id = text; return true;
    }

    internal static bool Text(JsonElement message, string name, int minimum, int maximum, out string value)
    {
        value = "";
        if (!message.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.String) return false;
        string? text = property.GetString();
        if (text is null || text.Length < minimum || text.Length > maximum) return false;
        foreach (var character in text) if (char.IsControl(character)) return false;
        value = text; return true;
    }

    // Exact key count plus the exact action name: an extra key (for example a path
    // supplied by the page) makes the request invalid instead of being ignored.
    internal static bool TryPassword(string action, JsonElement message, out string request, out string password)
    {
        password = "";
        if (!IsAction(message, action) || message.EnumerateObject().Count() != 3) { request = ""; return false; }
        return RequestId(message, out request) && Text(message, "password", 1, 256, out password);
    }

    internal static bool TryToken(JsonElement message, out string request, out string token)
    {
        token = "";
        if (!IsAction(message, "portable-restore") || message.EnumerateObject().Count() != 3) { request = ""; return false; }
        return RequestId(message, out request) && Text(message, "token", 1, 4096, out token);
    }

    internal static bool TryCancel(JsonElement message, out string request)
    {
        if (!IsAction(message, "portable-cancel") || message.EnumerateObject().Count() != 2) { request = ""; return false; }
        return RequestId(message, out request);
    }

    // ------------------------------------------------------------------- readers

    internal static bool Ok(JsonElement response) => response.ValueKind == JsonValueKind.Object
        && response.TryGetProperty("ok", out var value) && value.ValueKind == JsonValueKind.True;

    internal static JsonElement Result(JsonElement response) => response.ValueKind == JsonValueKind.Object
        && response.TryGetProperty("result", out var result) ? result : default;

    internal static string? Read(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.String ? property.GetString() : null;

    internal static long ReadNumber(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.Number
        && property.TryGetInt64(out var number) ? number : 0;

    static bool Flag(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(name, out var flag) && flag.ValueKind == JsonValueKind.True;

    internal static JsonElement Object(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.Object ? property : default;

    // session.status must report an unlocked session before any file picker opens.
    internal static bool Unlocked(JsonElement status) => Ok(status)
        && Result(status) is { ValueKind: JsonValueKind.Object } result
        && result.TryGetProperty("locked", out var locked) && locked.ValueKind == JsonValueKind.False;

    // ------------------------------------------------------------------- replies

    internal static JsonElement Failure(string code, string message) =>
        JsonSerializer.SerializeToElement(new { ok = false, error = code, message });

    // The service's own failure text is fixed and user facing (desktop_portable raises
    // AssistantError with a written message), so it is passed on; a local transport
    // failure never becomes user text.
    internal static JsonElement Refusal(JsonElement response, string fallback)
    {
        string? code = Read(response, "error");
        string? message = Read(response, "message");
        return Failure(string.IsNullOrEmpty(code) ? "portable_failed" : code!,
            string.IsNullOrWhiteSpace(message) ? fallback : message!);
    }

    // Only a file name leaves the window, even though the service already reports names.
    internal static string SafeName(string? value, string fallback)
    {
        if (string.IsNullOrWhiteSpace(value)) return fallback;
        string name = Path.GetFileName(value.Replace('\\', '/').Trim());
        if (string.IsNullOrWhiteSpace(name)) return fallback;
        foreach (var invalid in Path.GetInvalidFileNameChars()) name = name.Replace(invalid, '_');
        return name.Length > 120 ? name[..120] : name;
    }

    // export -> {backup,key_file,backup_bytes,key_bytes,backup_sha256,key_sha256,records,history}
    internal static JsonElement ExportReply(JsonElement response, string chosenPath)
    {
        var result = Result(response);
        return JsonSerializer.SerializeToElement(new
        {
            ok = true,
            result = new
            {
                backup = SafeName(Read(result, "backup"), SafeName(chosenPath, "便携备份.ccbackup")),
                key_file = SafeName(Read(result, "key_file"), ""),
                backup_bytes = ReadNumber(result, "backup_bytes"),
                key_bytes = ReadNumber(result, "key_bytes"),
                backup_sha256 = Read(result, "backup_sha256"),
                key_sha256 = Read(result, "key_sha256"),
                records = ReadNumber(result, "records"),
                history = ReadNumber(result, "history")
            }
        });
    }

    internal static JsonElement Preservation(JsonElement value)
    {
        var preservation = Object(value, "preservation");
        return JsonSerializer.SerializeToElement(new
        {
            name = SafeName(Read(preservation, "name"), ""),
            sha256 = Read(preservation, "sha256")
        });
    }

    // preview -> token, records, history, source{project,role,installation},
    // preservation{name,sha256}, effect. `counters` describes the encrypted store and is
    // deliberately not forwarded.
    internal static JsonElement PreviewReply(JsonElement response)
    {
        var result = Result(response);
        var source = Object(result, "source");
        return JsonSerializer.SerializeToElement(new
        {
            ok = true,
            result = new
            {
                token = Read(result, "token"),
                records = ReadNumber(result, "records"),
                history = ReadNumber(result, "history"),
                source = new
                {
                    project = Read(source, "project"),
                    role = Read(source, "role"),
                    installation = Read(source, "installation")
                },
                preservation = Preservation(result),
                effect = Read(result, "effect")
            }
        });
    }

    // complete -> switched, restart_required, preservation, and this window's restart
    // instruction. The stager report carries generation store paths, so it never leaves the
    // window, and neither does the worker's database name.
    internal static JsonElement SwitchReply(JsonElement response, bool restart, string message)
    {
        var result = Result(response);
        bool switched = Flag(result, "switched");
        return JsonSerializer.SerializeToElement(new
        {
            ok = true,
            result = new
            {
                switched,
                restart_required = switched || restart || Flag(result, "restart_required"),
                preservation = Preservation(result),
                message
            }
        });
    }

    internal static async Task<JsonElement> Export(Exchange exchange, string path, string password, string chosenPath)
    {
        var response = await exchange(ExportOperation, new { path, password });
        if (!Ok(response)) return Refusal(response, "便携备份未导出，请检查所选位置后重试。");
        return ExportReply(response, chosenPath);
    }

    internal static async Task<JsonElement> Preview(Exchange exchange, string backup, string keyFile, string password)
    {
        var response = await exchange(PreviewOperation, new { backup, key_file = keyFile, password });
        if (!Ok(response)) return Refusal(response, "便携备份或独立密钥文件无法读取，请确认这一对文件与恢复口令后重试。");
        if (string.IsNullOrEmpty(Read(Result(response), "token")))
            return Failure("portable_protocol", "后台没有返回导入预览凭证，当前资料未改变。请重新选择这一对文件。");
        return PreviewReply(response);
    }

    internal static async Task<JsonElement> Cancel(Exchange exchange)
    {
        var response = await exchange(CancelOperation, new { });
        if (!Ok(response)) return Refusal(response, "未能通知后台取消本次导入，当前资料未改变；请重新启动后台后再试。");
        return JsonSerializer.SerializeToElement(new { ok = true, result = new { cancelled = true } });
    }

    // Decides what the host may do with one complete attempt. A missing reply is unknown
    // once complete was sent; before that nothing was submitted.
    internal static PortableSwitchOutcome Classify(JsonElement response, bool completeSent)
    {
        if (response.ValueKind != JsonValueKind.Object) return completeSent ? PortableSwitchOutcome.Uncertain : PortableSwitchOutcome.NotStarted;
        if (Ok(response))
            return Flag(Result(response), "switched") ? PortableSwitchOutcome.Completed : PortableSwitchOutcome.Uncertain;
        string code = Read(response, "error") ?? "";
        return Array.IndexOf(UncertainSwitchCodes, code) >= 0 ? PortableSwitchOutcome.Uncertain : PortableSwitchOutcome.Refused;
    }
}

// Local preservation listing: the pre-replacement copies this installation keeps under
// history/portable-preservation. It rides the same private desktop-portable envelope and the
// same final Windows confirmation as a portable import: the page sends only the continuation
// cursor or the opaque handle the backend issued, never a path, a generation name or a key.
internal static class DesktopPreserved
{
    internal const string ListOperation = "preserved-list";
    internal const string PreviewOperation = "preserved-preview";
    internal const string ListAction = "portable-preserved-list";
    internal const string PreviewAction = "portable-preserved-preview";
    internal const int MaxItems = 400;

    // {action:'portable-preserved-list', request_id, after}: `after` is the previous cursor
    // or empty for the first page. Nothing else may be present.
    internal static bool TryList(JsonElement message, out string request, out string after)
    {
        after = "";
        if (!DesktopPortable.IsAction(message, ListAction) || message.EnumerateObject().Count() != 3)
        { request = ""; return false; }
        return DesktopPortable.RequestId(message, out request)
            && DesktopPortable.Text(message, "after", 0, 4096, out after);
    }

    internal static bool TryPreview(JsonElement message, out string request, out string handle)
    {
        handle = "";
        if (!DesktopPortable.IsAction(message, PreviewAction) || message.EnumerateObject().Count() != 3)
        { request = ""; return false; }
        return DesktopPortable.RequestId(message, out request)
            && DesktopPortable.Text(message, "handle", 1, 4096, out handle);
    }

    internal static async Task<JsonElement> List(DesktopPortable.Exchange exchange, string after)
    {
        var response = await exchange(ListOperation, new { after });
        if (!DesktopPortable.Ok(response)) return DesktopPortable.Refusal(response, "未能读取本机保全资料，请稍后重试。");
        return ListReply(response);
    }

    internal static async Task<JsonElement> Preview(DesktopPortable.Exchange exchange, string handle)
    {
        var response = await exchange(PreviewOperation, new { handle });
        if (!DesktopPortable.Ok(response)) return DesktopPortable.Refusal(response, "这份本机保全资料无法读取，请重新读取列表后再试。");
        if (string.IsNullOrEmpty(DesktopPortable.Read(DesktopPortable.Result(response), "token")))
            return DesktopPortable.Failure("portable_protocol", "后台没有返回保全资料预览凭证，当前资料未改变。请重新读取列表。");
        return PreviewReply(response);
    }

    // Only the handle, ordinary file metadata and the continuation cursor leave the window.
    internal static JsonElement ListReply(JsonElement response)
    {
        var result = DesktopPortable.Result(response);
        var items = new List<object>();
        if (result.ValueKind == JsonValueKind.Object
            && result.TryGetProperty("items", out var listed) && listed.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in listed.EnumerateArray())
            {
                if (items.Count == MaxItems) break;
                string handle = DesktopPortable.Read(item, "handle") ?? "";
                if (handle.Length == 0) continue;
                items.Add(new
                {
                    handle,
                    name = DesktopPortable.SafeName(DesktopPortable.Read(item, "name"), "保全资料"),
                    taken_at = DesktopPortable.Read(item, "taken_at") ?? "",
                    bytes = DesktopPortable.ReadNumber(item, "bytes"),
                    usable = Usable(item)
                });
            }
        }
        return JsonSerializer.SerializeToElement(new
        {
            ok = true,
            result = new { items, next = DesktopPortable.Read(result, "next") ?? "" }
        });
    }

    // Only an explicit JSON true marks a candidate as usable: a missing value, a string, a
    // number or any other kind is treated as not usable, so a malformed reply can never offer
    // a copy for confirmation.
    static bool Usable(JsonElement item) => item.ValueKind == JsonValueKind.Object
        && item.TryGetProperty("usable", out var usable) && usable.ValueKind == JsonValueKind.True;

    // preserved-preview -> token, kind, records, history, name, taken_at,
    // source{project,role,installation}, preservation{name,sha256}, effect.
    internal static JsonElement PreviewReply(JsonElement response)
    {
        var result = DesktopPortable.Result(response);
        var source = DesktopPortable.Object(result, "source");
        return JsonSerializer.SerializeToElement(new
        {
            ok = true,
            result = new
            {
                token = DesktopPortable.Read(result, "token"),
                kind = "preserved",
                records = DesktopPortable.ReadNumber(result, "records"),
                history = DesktopPortable.ReadNumber(result, "history"),
                name = DesktopPortable.SafeName(DesktopPortable.Read(result, "name"), ""),
                taken_at = DesktopPortable.Read(result, "taken_at") ?? "",
                source = new
                {
                    project = DesktopPortable.Read(source, "project"),
                    role = DesktopPortable.Read(source, "role"),
                    installation = DesktopPortable.Read(source, "installation")
                },
                preservation = DesktopPortable.Preservation(result),
                effect = DesktopPortable.Read(result, "effect")
            }
        });
    }
}

// The replacement handshake. Verification is supplied by this window only, exactly one
// complete may ever cross the pipe, and cancel never follows complete.
internal sealed class PortableRestore(DesktopPortable.Exchange exchange, Func<CancellationToken, Task<string>> verify)
{
    internal bool CompleteSent { get; private set; }
    internal bool CancelSent { get; private set; }

    internal async Task<JsonElement> Run(string token, CancellationToken lifetime)
    {
        var start = await exchange(DesktopPortable.BeginOperation, new { token });
        // A refused begin staged nothing: there is no challenge to complete.
        if (!DesktopPortable.Ok(start)) return DesktopPortable.Refusal(start, "后台未接受本次恢复，当前资料未改变。请重新预览后再试。");
        string? challenge = DesktopPortable.Read(DesktopPortable.Result(start), "challenge");
        if (string.IsNullOrEmpty(challenge))
        {
            await SendCancel();
            return DesktopPortable.Failure("portable_protocol", "后台没有返回恢复授权，当前资料未改变。请重新预览后再试。");
        }
        // Windows verification has its own limit inside the flow budget; a timeout is
        // treated exactly like a cancelled prompt: cancel, never complete.
        string verified;
        using (var windows = CancellationTokenSource.CreateLinkedTokenSource(lifetime))
        {
            windows.CancelAfter(TimeSpan.FromSeconds(DesktopPortable.WindowsSeconds));
            try { verified = await verify(windows.Token); }
            catch (OperationCanceledException) { verified = "Canceled"; }
        }
        if (verified != "Verified")
        {
            await SendCancel();
            return DesktopPortable.Failure("windows_verification", WindowsVerification.RestoreMessage(verified));
        }
        if (lifetime.IsCancellationRequested)
        {
            await SendCancel();
            lifetime.ThrowIfCancellationRequested();
        }
        CompleteSent = true;
        return await exchange(DesktopPortable.CompleteOperation, new { challenge });
    }

    async Task SendCancel()
    {
        if (CancelSent || CompleteSent) return;
        CancelSent = true;
        try { await exchange(DesktopPortable.CancelOperation, new { }); }
        catch (Exception error) when (error is IOException or OperationCanceledException) { }
    }
}
