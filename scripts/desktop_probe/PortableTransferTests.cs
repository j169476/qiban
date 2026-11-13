// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;

// Native flow checks for the portable cross-machine transfer. Everything here is
// orchestration only: no Windows prompt is shown, no file is read and no backend is
// contacted, so these cases run without a database and without touching real资料.
// The reply fixtures are the exact shapes src/qiban/desktop_portable.py returns.
internal static class PortableTransferTests
{
    const string BackupName = "qiban-便携备份-20261107.ccbackup";
    const string KeyName = "qiban-便携备份-20261107.cckey";
    const string PreservationName = "4f2a9c0d5e6b7a81ccbackup.ccbackup";
    const string Password = "Synthetic-Portable-Recovery-2026";
    static readonly string Digest = new('a', 64);

    static JsonElement Ok(object result) => JsonSerializer.SerializeToElement(new { ok = true, result });
    static JsonElement Failure(string code, string message) =>
        JsonSerializer.SerializeToElement(new { ok = false, error = code, message });
    static JsonElement Begin(string? challenge) => Ok(challenge is null ? new { } : (object)new { challenge });

    // desktop_portable.export()
    static JsonElement ExportResult() => Ok(new
    {
        backup = BackupName, key_file = KeyName,
        backup_sha256 = Digest, key_sha256 = new string('b', 64),
        backup_bytes = 4096, key_bytes = 300,
        records = 12, history = 34, encrypted = true
    });

    // desktop_portable.preview(): `counters` are encrypted-store statistics.
    static JsonElement PreviewResult() => Ok(new
    {
        token = "synthetic-preview-token", expires_in_seconds = 600,
        records = 12, history = 34,
        counters = new
        {
            current_records = 12, history_records = 34, encrypted_records = 120, ledger_envelopes = 47,
            tables = new { content_records = 12, content_history = 34 }, assets = 3
        },
        source = new { project = "synthetic-proj", role = "合成角色", installation = "synthetic-ins" },
        preservation = new { name = PreservationName, sha256 = Digest },
        effect = "将完整替换当前资料与历史，两份资料不合并。"
    });

    // desktop_portable.complete(): the stager report carries generation store paths.
    static JsonElement SwitchResult() => Ok(new
    {
        stager = new
        {
            generation = "generation-2", restart_required = true, restored = true,
            store = @"C:\synthetic\installation\generations\generation-2\store",
            database = "qiban_restore_synthetic"
        },
        switched = true, restored = true, restart_required = true,
        preservation = new { name = PreservationName, sha256 = Digest }
    });

    // desktop_portable.preserved_list(): file metadata only, plus the fields the window must
    // drop (a key slot, a store path, a digest).
    static JsonElement PreservedListResult() => Ok(new
    {
        items = new object[]
        {
            new
            {
                handle = "synthetic-handle-a", name = "aaaa.ccbackup",
                taken_at = "2026-11-07T10:11:12+00:00", bytes = 4096, usable = true,
                keyref = "generation-2",
                path = @"C:\synthetic\store\history\portable-preservation\aaaa.ccbackup",
                sha256 = new string('c', 64)
            },
            new
            {
                handle = "synthetic-handle-b", name = "bbbb.ccbackup",
                taken_at = "2026-11-07T09:00:00+00:00", bytes = 0, usable = false,
                keyref = "aaaa0000aaaa0000aaaa0000aaaa0000"
            }
        },
        next = "synthetic-cursor"
    });

    // desktop_portable.preserved_preview(): the same authenticated preview payload, with its
    // own ordinary file name and time, and no encrypted-store counters.
    static JsonElement PreservedPreviewResult() => Ok(new
    {
        token = "synthetic-preserved-token", expires_in_seconds = 600, kind = "preserved",
        records = 12, history = 34, name = "aaaa.ccbackup",
        taken_at = "2026-11-07T10:11:12+00:00", keyref = "generation-2",
        counters = new { ciphertexts = 120, ledger = 47 },
        source = new { project = "synthetic-proj", role = "合成角色", installation = "synthetic-ins" },
        preservation = new { name = PreservationName, sha256 = Digest },
        effect = "将完整替换当前资料与历史，两份资料不合并。"
    });

    static async Task<bool> Throws<T>(Func<Task> action) where T : Exception
    {
        try { await action(); }
        catch (T) { return true; }
        return false;
    }

    internal static async Task<int> Verify(string evidence)
    {
        bool passed = false;
        string[] cases = [];
        string? failure = null;
        try { cases = await Run(); passed = true; }
        catch (Exception error) { failure = error.GetType().Name + ":" + error.Message; }
        Directory.CreateDirectory(evidence);
        File.WriteAllText(Path.Combine(evidence, "portable-result.json"),
            JsonSerializer.Serialize(new { passed, cases, failure }));
        return passed ? 0 : 1;
    }

    internal static async Task<string[]> Run()
    {
        var cases = new List<string>();
        // 1. Verification gate: the switch completes at most once, and every other
        //    Windows result cancels instead of completing.
        foreach (string result in new[]{"Verified","Canceled","NotConfiguredForUser","DeviceBusy","DisabledByPolicy",
            "RetriesExhausted","Unavailable","cancel-token"})
        {
            var operations = new List<string>();
            Task<JsonElement> Exchange(string operation, object arguments)
            {
                operations.Add(operation);
                return Task.FromResult(operation == "begin" ? Begin("test-challenge") : SwitchResult());
            }
            var flow = new PortableRestore(Exchange, _ => result == "cancel-token"
                ? Task.FromException<string>(new OperationCanceledException()) : Task.FromResult(result));
            var response = await flow.Run("test-token", CancellationToken.None);
            bool verified = result == "Verified";
            if (!operations.SequenceEqual(verified ? new[]{"begin","complete"} : new[]{"begin","cancel"}))
                throw new IOException("portable_gate_sequence_failed");
            if (operations.Count(operation => operation == "complete") != (verified ? 1 : 0))
                throw new IOException("portable_complete_not_unique");
            if (DesktopPortable.Ok(response) != verified) throw new IOException("portable_gate_outcome_failed");
            if (flow.CompleteSent != verified || flow.CancelSent != !verified) throw new IOException("portable_gate_flags_failed");
            if (verified && DesktopPortable.Classify(response, true) != PortableSwitchOutcome.Completed)
                throw new IOException("portable_verified_switch_not_classified_completed");
            cases.Add("portable_verification_gate_" + result);
        }
        // 2. A refused begin stages nothing: no Windows prompt, no cancel, no complete.
        {
            var operations = new List<string>();
            bool prompted = false;
            var flow = new PortableRestore((operation, _) =>
            {
                operations.Add(operation);
                return Task.FromResult(Failure("portable_preview_expired","便携恢复预览已失效，请重新查看"));
            }, _ => { prompted = true; return Task.FromResult("Verified"); });
            var response = await flow.Run("test-token", CancellationToken.None);
            if (prompted || operations.Count != 1 || operations[0] != "begin") throw new IOException("portable_refused_begin_continued");
            if (DesktopPortable.Ok(response) || flow.CompleteSent) throw new IOException("portable_refused_begin_completed");
            if (DesktopPortable.Classify(response, false) != PortableSwitchOutcome.Refused)
                throw new IOException("portable_expired_preview_not_refused");
            cases.Add("portable_refused_begin_prompts_and_completes_nothing");
        }
        // 3. A begin that answers without a challenge is cancelled, never completed.
        {
            var operations = new List<string>();
            var flow = new PortableRestore((operation, _) =>
            {
                operations.Add(operation);
                return Task.FromResult(operation == "begin" ? Begin(null) : SwitchResult());
            }, _ => Task.FromResult("Verified"));
            var response = await flow.Run("test-token", CancellationToken.None);
            if (!operations.SequenceEqual(new[]{"begin","cancel"}) || flow.CompleteSent) throw new IOException("portable_missing_challenge_completed");
            if (DesktopPortable.Read(response, "error") != "portable_protocol") throw new IOException("portable_missing_challenge_reported_wrongly");
            cases.Add("portable_missing_challenge_cancels_without_complete");
        }
        // 4. Cancellation before complete sends cancel and never completes, even after a
        //    verified prompt.
        {
            using var cancelled = new CancellationTokenSource();
            cancelled.Cancel();
            var operations = new List<string>();
            var flow = new PortableRestore((operation, _) =>
            {
                operations.Add(operation);
                return Task.FromResult(operation == "begin" ? Begin("test-challenge") : SwitchResult());
            }, _ => Task.FromResult("Verified"));
            if (!await Throws<OperationCanceledException>(() => flow.Run("test-token", cancelled.Token)))
                throw new IOException("portable_cancellation_not_reported");
            if (!operations.SequenceEqual(new[]{"begin","cancel"}) || flow.CompleteSent || !flow.CancelSent)
                throw new IOException("portable_cancellation_completed");
            cases.Add("portable_cancel_precedes_complete_after_cancellation");
        }
        // 5. A lost reply after complete is an unknown outcome: the old backend must be
        //    stopped and nothing may be resubmitted.
        {
            var operations = new List<string>();
            var flow = new PortableRestore((operation, _) =>
            {
                operations.Add(operation);
                if (operation == "begin") return Task.FromResult(Begin("test-challenge"));
                throw new IOException("portable_lost_reply");
            }, _ => Task.FromResult("Verified"));
            if (!await Throws<IOException>(() => flow.Run("test-token", CancellationToken.None)))
                throw new IOException("portable_lost_reply_swallowed");
            if (!operations.SequenceEqual(new[]{"begin","complete"}) || !flow.CompleteSent || flow.CancelSent)
                throw new IOException("portable_lost_reply_state_wrong");
            if (DesktopPortable.Classify(default, true) != PortableSwitchOutcome.Uncertain
                || DesktopPortable.Classify(default, false) != PortableSwitchOutcome.NotStarted)
                throw new IOException("portable_missing_reply_classified_wrongly");
            cases.Add("portable_lost_complete_reply_is_uncertain_and_stops_backend");
        }
        // 6. Reply classification decides whether the host stops the old backend.
        {
            var stopped = new[]{"portable_switch_uncertain","portable_switch_applied","portable_switched",
                "portable_outcome_unknown","portable_failed"};
            foreach (var code in stopped)
                if (DesktopPortable.Classify(Failure(code, "固定消息"), true) != PortableSwitchOutcome.Uncertain)
                    throw new IOException("portable_uncertain_code_not_uncertain:" + code);
            var kept = new[]{"portable_verification_expired","portable_preview_expired","portable_changed",
                "portable_path","portable_missing","portable_limit","portable_schema","portable_source",
                "portable_preserve","recovery_key","backup_invalid","portable_unavailable","portable_key","management_request"};
            foreach (var code in kept)
                if (DesktopPortable.Classify(Failure(code, "固定消息"), true) != PortableSwitchOutcome.Refused)
                    throw new IOException("portable_precommit_code_stopped_backend:" + code);
            if (DesktopPortable.Classify(Ok(new {switched = true, restart_required = true}), true) != PortableSwitchOutcome.Completed)
                throw new IOException("portable_switched_not_completed");
            foreach (var incomplete in new[]{
                Ok(new {switched = false}),
                Ok(new {switched = false, restart_required = false}),
                Ok(new {switched = false, restart_required = true})})
                if (DesktopPortable.Classify(Ok(incomplete), true) != PortableSwitchOutcome.Uncertain)
                    throw new IOException("portable_unconfirmed_switch_not_uncertain");
            cases.Add("portable_reply_classification_stops_only_after_a_possible_switch");
        }
        // 7. Page requests: exact key count, action and request id, and never a path.
        {
            string id = Guid.NewGuid().ToString("N");
            JsonElement Request(params (string Name, object Value)[] fields)
            {
                var values = new Dictionary<string, object>();
                foreach (var (name, value) in fields) values[name] = value;
                return JsonSerializer.SerializeToElement(values);
            }
            if (!DesktopPortable.TryPassword("portable-export",
                Request(("action","portable-export"),("request_id",id),("password",Password)), out _, out var password)
                || password != Password) throw new IOException("portable_password_request_rejected");
            if (DesktopPortable.TryPassword("portable-preview",
                Request(("action","portable-preview"),("request_id",id),("password","x"),("path",@"C:\synthetic\a.ccbackup")), out _, out _))
                throw new IOException("portable_page_path_accepted");
            if (DesktopPortable.TryPassword("portable-export",
                Request(("action","portable-preview"),("request_id",id),("password","x")), out _, out _))
                throw new IOException("portable_wrong_action_accepted");
            if (!DesktopPortable.TryPassword("portable-preview",
                Request(("action","portable-preview"),("request_id",id),("password",Password)), out _, out _))
                throw new IOException("portable_preview_password_rejected");
            if (DesktopPortable.TryPassword("portable-export",
                Request(("action","portable-export"),("request_id",id.ToUpperInvariant()),("password","x")), out _, out _))
                throw new IOException("portable_upper_request_id_accepted");
            if (DesktopPortable.TryPassword("portable-export",
                Request(("action","portable-export"),("request_id",id[..31]),("password","x")), out _, out _))
                throw new IOException("portable_short_request_id_accepted");
            if (!DesktopPortable.TryToken(
                Request(("action","portable-restore"),("request_id",id),("token","synthetic-token")), out _, out var token)
                || token != "synthetic-token") throw new IOException("portable_token_request_rejected");
            if (DesktopPortable.TryToken(
                Request(("action","portable-restore"),("request_id",id),("password","x")), out _, out _))
                throw new IOException("portable_password_as_token_accepted");
            if (!DesktopPortable.TryCancel(Request(("action","portable-cancel"),("request_id",id)), out _))
                throw new IOException("portable_cancel_request_rejected");
            if (DesktopPortable.TryCancel(Request(("action","portable-cancel"),("request_id",id),("token","t")), out _))
                throw new IOException("portable_cancel_extra_key_accepted");
            cases.Add("portable_page_requests_require_exact_keys_and_never_a_path");
        }
        // 8. Export maps the service fields the page shows: the pair's names and sizes,
        //    both digests and both record counts. Nothing else crosses, and no absolute
        //    path survives.
        {
            const string chosen = @"C:\synthetic\private\qiban-便携备份-20261107.ccbackup";
            string? operation = null;
            JsonElement arguments = default;
            var exported = await DesktopPortable.Export((name, values) =>
            {
                operation = name; arguments = JsonSerializer.SerializeToElement(values);
                return Task.FromResult(ExportResult());
            }, chosen, Password, chosen);
            var result = DesktopPortable.Result(exported);
            if (operation != "export" || arguments.GetProperty("path").GetString() != chosen
                || arguments.GetProperty("password").GetString() != Password)
                throw new IOException("portable_export_request_wrong");
            if (!DesktopPortable.Ok(exported)
                || DesktopPortable.Read(result, "backup") != BackupName
                || DesktopPortable.Read(result, "key_file") != KeyName
                || DesktopPortable.ReadNumber(result, "backup_bytes") != 4096
                || DesktopPortable.ReadNumber(result, "key_bytes") != 300
                || DesktopPortable.Read(result, "backup_sha256") != Digest
                || DesktopPortable.ReadNumber(result, "records") != 12
                || DesktopPortable.ReadNumber(result, "history") != 34)
                throw new IOException("portable_export_fields_missing");
            var names = result.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!names.SequenceEqual(new[]{"backup","backup_bytes","backup_sha256","history","key_bytes","key_file","key_sha256","records"}))
                throw new IOException("portable_export_fields_not_whitelisted:" + string.Join(',', names));
            if (exported.GetRawText().Contains("synthetic", StringComparison.OrdinalIgnoreCase)
                || exported.GetRawText().Contains(Password, StringComparison.Ordinal))
                throw new IOException("portable_export_reply_leaked");
            var absolute = await DesktopPortable.Export((_, _) => Task.FromResult(Ok(new
            {
                backup = chosen, key_file = @"C:\synthetic\private\" + KeyName,
                backup_bytes = 1, key_bytes = 1, backup_sha256 = Digest, key_sha256 = Digest, records = 0, history = 0
            })), chosen, Password, chosen);
            if (DesktopPortable.Read(DesktopPortable.Result(absolute), "backup") != Path.GetFileName(chosen)
                || absolute.GetRawText().Contains("private", StringComparison.OrdinalIgnoreCase))
                throw new IOException("portable_export_reply_leaked_path");
            cases.Add("portable_export_maps_service_fields_and_drops_the_rest");
        }
        // 9. Preview maps records/history plus the source labels and the preservation
        //    backup, and never the encrypted-store counters.
        {
            const string backup = @"\\synthetic-host\share\portable.ccbackup", key = @"\\synthetic-host\share\portable.cckey";
            string? operation = null;
            JsonElement arguments = default;
            var preview = await DesktopPortable.Preview((name, values) =>
            {
                operation = name; arguments = JsonSerializer.SerializeToElement(values);
                return Task.FromResult(PreviewResult());
            }, backup, key, Password);
            var result = DesktopPortable.Result(preview);
            if (operation != "preview" || arguments.GetProperty("backup").GetString() != backup
                || arguments.GetProperty("key_file").GetString() != key
                || arguments.GetProperty("password").GetString() != Password)
                throw new IOException("portable_preview_request_wrong");
            if (DesktopPortable.Read(result, "token") != "synthetic-preview-token"
                || DesktopPortable.ReadNumber(result, "records") != 12
                || DesktopPortable.ReadNumber(result, "history") != 34
                || DesktopPortable.Read(result.GetProperty("source"), "role") != "合成角色"
                || DesktopPortable.Read(result.GetProperty("source"), "project") != "synthetic-proj"
                || DesktopPortable.Read(result.GetProperty("source"), "installation") != "synthetic-ins"
                || DesktopPortable.Read(result.GetProperty("preservation"), "name") != PreservationName
                || DesktopPortable.Read(result.GetProperty("preservation"), "sha256") != Digest
                || DesktopPortable.Read(result, "effect") != "将完整替换当前资料与历史，两份资料不合并。")
                throw new IOException("portable_preview_fields_missing");
            var names = result.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!names.SequenceEqual(new[]{"effect","history","preservation","records","source","token"}))
                throw new IOException("portable_preview_fields_not_whitelisted:" + string.Join(',', names));
            if (preview.GetRawText().Contains("counters", StringComparison.Ordinal)
                || preview.GetRawText().Contains("encrypted_records", StringComparison.Ordinal)
                || preview.GetRawText().Contains("synthetic-host", StringComparison.OrdinalIgnoreCase)
                || preview.GetRawText().Contains(Password, StringComparison.Ordinal))
                throw new IOException("portable_preview_reply_leaked");
            var missing = await DesktopPortable.Preview((_, _) => Task.FromResult(Ok(new{records = 1})), backup, key, Password);
            if (DesktopPortable.Ok(missing) || DesktopPortable.Read(missing, "error") != "portable_protocol")
                throw new IOException("portable_preview_token_not_required");
            cases.Add("portable_preview_maps_records_source_preservation_only");
        }
        // 10. Cancel sends exactly one empty cancel request and claims no switch.
        {
            string? operation = null;
            int keys = -1;
            var cancelled = await DesktopPortable.Cancel((name, values) =>
            {
                operation = name; keys = JsonSerializer.SerializeToElement(values).EnumerateObject().Count();
                return Task.FromResult(Ok(new{cancelled = true}));
            });
            if (operation != "cancel" || keys != 0 || !DesktopPortable.Ok(cancelled)) throw new IOException("portable_cancel_request_wrong");
            var refused = await DesktopPortable.Cancel((_, _) => Task.FromResult(Failure("portable_unavailable","备份服务尚未就绪")));
            if (DesktopPortable.Ok(refused) || DesktopPortable.Read(refused, "error") != "portable_unavailable")
                throw new IOException("portable_cancel_failure_reported_as_success");
            cases.Add("portable_cancel_sends_one_empty_cancel");
        }
        // 11. Switch replies show the outcome and the preservation backup, never the
        //     stager report (which carries generation store paths).
        {
            var switched = DesktopPortable.SwitchReply(SwitchResult(), true, "请点击『启动验证服务』");
            var result = DesktopPortable.Result(switched);
            var names = result.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!names.SequenceEqual(new[]{"message","preservation","restart_required","switched"}))
                throw new IOException("portable_switch_fields_not_whitelisted:" + string.Join(',', names));
            if (!result.GetProperty("switched").GetBoolean() || !result.GetProperty("restart_required").GetBoolean()
                || DesktopPortable.Read(result.GetProperty("preservation"), "name") != PreservationName)
                throw new IOException("portable_switch_reply_wrong");
            if (switched.GetRawText().Contains("generation-2", StringComparison.Ordinal)
                || switched.GetRawText().Contains("stager", StringComparison.Ordinal)
                || switched.GetRawText().Contains("qiban_restore", StringComparison.Ordinal)
                || switched.GetRawText().Contains("synthetic", StringComparison.OrdinalIgnoreCase))
                throw new IOException("portable_switch_reply_leaked_stager_report");
            var uncertain = DesktopPortable.SwitchReply(Failure("portable_switch_uncertain","切换步骤已提交但结果未知"),
                true, "恢复结果未知");
            var unknown = DesktopPortable.Result(uncertain);
            if (unknown.GetProperty("switched").GetBoolean() || !unknown.GetProperty("restart_required").GetBoolean())
                throw new IOException("portable_uncertain_switch_reply_wrong");
            cases.Add("portable_switch_reply_reports_outcome_not_stager");
        }
        // 12. A locked session never opens a picker.
        {
            if (!DesktopPortable.Unlocked(Ok(new{locked = false, lock_enabled = true}))
                || DesktopPortable.Unlocked(Ok(new{locked = true, lock_enabled = true}))
                || DesktopPortable.Unlocked(Failure("desktop_locked","已锁定")))
                throw new IOException("portable_session_lock_gate_wrong");
            cases.Add("portable_locked_session_blocks_picker");
        }
        // 13. Deadline contract: begin, Windows verification and complete each have a full
        //     budget, and ordinary requests keep theirs.
        {
            var begin = DesktopPortable.Envelope(DesktopPortable.BeginOperation, new{token = "synthetic"});
            var complete = DesktopPortable.Envelope(DesktopPortable.CompleteOperation, new{challenge = "synthetic"});
            if (StorageComponent.ReplySecondsForTest(begin) != 240 || StorageComponent.CallMillisecondsForTest(begin) != 245000
                || StorageComponent.ReplySecondsForTest(complete) != 240 || StorageComponent.CallMillisecondsForTest(complete) != 245000)
                throw new IOException("portable_deadline_unmatched");
            var cancel = DesktopPortable.Envelope(DesktopPortable.CancelOperation, new{});
            if (StorageComponent.ReplySecondsForTest(cancel) != 60 || StorageComponent.CallMillisecondsForTest(cancel) != 65000)
                throw new IOException("portable_cancel_deadline_unmatched");
            if (DesktopPortable.RestoreTimeout != TimeSpan.FromSeconds(660))
                throw new IOException("portable_restore_timeout_does_not_cover_begin_windows_complete");
            var ordinary = JsonSerializer.SerializeToElement(new{action = "management", operation = "config.read", arguments = new{kind = "persona"}});
            if (StorageComponent.ReplySecondsForTest(ordinary) != 20 || StorageComponent.CallMillisecondsForTest(ordinary) != 10000)
                throw new IOException("ordinary_request_deadline_changed");
            var backup = JsonSerializer.SerializeToElement(new{action = "management", operation = "backups.create", arguments = new{}});
            if (StorageComponent.ReplySecondsForTest(backup) != 120 || StorageComponent.CallMillisecondsForTest(backup) != 125000)
                throw new IOException("backup_request_deadline_changed");
            cases.Add("portable_deadlines_matched_and_ordinary_requests_unchanged");
        }
        // 14. Only a file name leaves the window.
        {
            if (DesktopPortable.SafeName(@"C:\synthetic\a.ccbackup", "fallback") != "a.ccbackup"
                || DesktopPortable.SafeName("/synthetic/a.cckey", "fallback") != "a.cckey"
                || DesktopPortable.SafeName("", "fallback") != "fallback"
                || DesktopPortable.SafeName(@"C:\synthetic\", "fallback") != "fallback")
                throw new IOException("portable_file_name_not_reduced");
            cases.Add("portable_reply_reduces_paths_to_file_names");
        }
        // 15. Preserved listing requests: exact key count, no path, no password, no key slot.
        {
            string id = Guid.NewGuid().ToString("N");
            JsonElement Request(params (string Name, object Value)[] fields)
            {
                var values = new Dictionary<string, object>();
                foreach (var (name, value) in fields) values[name] = value;
                return JsonSerializer.SerializeToElement(values);
            }
            if (!DesktopPreserved.TryList(
                Request(("action","portable-preserved-list"),("request_id",id),("after","")), out _, out var first)
                || first != "") throw new IOException("preserved_list_request_rejected");
            if (!DesktopPreserved.TryList(
                Request(("action","portable-preserved-list"),("request_id",id),("after","synthetic-cursor")), out _, out var after)
                || after != "synthetic-cursor") throw new IOException("preserved_cursor_rejected");
            foreach (var rejected in new[]{
                Request(("action","portable-preserved-list"),("request_id",id),("after",""),("password","x")),
                Request(("action","portable-preserved-list"),("request_id",id),("after",""),("path",@"C:\synthetic")),
                Request(("action","portable-preserved-list"),("request_id",id)),
                Request(("action","portable-preserved-list"),("request_id",id.ToUpperInvariant()),("after",""))})
                if (DesktopPreserved.TryList(rejected, out _, out _))
                    throw new IOException("preserved_list_request_not_exact");
            if (!DesktopPreserved.TryPreview(
                Request(("action","portable-preserved-preview"),("request_id",id),("handle","synthetic-handle")), out _, out var handle)
                || handle != "synthetic-handle") throw new IOException("preserved_preview_request_rejected");
            foreach (var rejected in new[]{
                Request(("action","portable-preserved-preview"),("request_id",id),("handle","")),
                Request(("action","portable-preserved-preview"),("request_id",id),("handle","h"),("password","x")),
                Request(("action","portable-preserved-preview"),("request_id",id),("handle","h"),("keyref","generation-2")),
                Request(("action","portable-preserved-preview"),("request_id",id),("handle","h"),("path",@"C:\synthetic")),
                Request(("action","portable-preserved-preview"),("request_id",id))})
                if (DesktopPreserved.TryPreview(rejected, out _, out _))
                    throw new IOException("preserved_preview_request_not_exact");
            cases.Add("preserved_requests_are_exact_and_carry_no_path_or_password");
        }
        // 16. Preserved listing replies: only handle and ordinary file metadata leave the window.
        {
            var listed = DesktopPreserved.ListReply(PreservedListResult());
            var result = DesktopPortable.Result(listed);
            var names = result.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!names.SequenceEqual(new[]{"items","next"})) throw new IOException("preserved_list_fields_not_whitelisted");
            if (DesktopPortable.Read(result, "next") != "synthetic-cursor") throw new IOException("preserved_cursor_not_forwarded");
            var items = result.GetProperty("items");
            if (items.GetArrayLength() != 2) throw new IOException("preserved_items_lost");
            foreach (var item in items.EnumerateArray())
            {
                var itemNames = item.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
                if (!itemNames.SequenceEqual(new[]{"bytes","handle","name","taken_at","usable"}))
                    throw new IOException("preserved_item_fields_not_whitelisted");
            }
            if (items[0].GetProperty("usable").GetBoolean() != true || items[1].GetProperty("usable").GetBoolean() != false)
                throw new IOException("preserved_usable_flag_lost");
            if (DesktopPortable.SafeName(DesktopPortable.Read(items[0], "name"), "") != "aaaa.ccbackup")
                throw new IOException("preserved_name_wrong");
            var raw = listed.GetRawText();
            foreach (var leaked in new[]{@"C:\synthetic", "portable-preservation", "generation-2",
                new string('c', 64), "keyref", "sha256"})
                if (raw.Contains(leaked, StringComparison.Ordinal))
                    throw new IOException("preserved_list_reply_leaked:" + leaked);
            cases.Add("preserved_list_reply_is_metadata_only");
        }
        // 16b. Only a JSON true marks a copy usable; any other kind is not offered.
        {
            var mapping = DesktopPreserved.ListReply(Ok(new
            {
                items = new object[]
                {
                    new { handle = "h-true", name = "a.ccbackup", taken_at = "t", bytes = 1, usable = true },
                    new { handle = "h-false", name = "b.ccbackup", taken_at = "t", bytes = 1, usable = false },
                    new { handle = "h-missing", name = "c.ccbackup", taken_at = "t", bytes = 1 },
                    new { handle = "h-string", name = "d.ccbackup", taken_at = "t", bytes = 1, usable = "true" },
                    new { handle = "h-number", name = "e.ccbackup", taken_at = "t", bytes = 1, usable = 1 },
                    new { handle = "h-null", name = "f.ccbackup", taken_at = "t", bytes = 1, usable = (string?)null }
                },
                next = ""
            }));
            var flags = DesktopPortable.Result(mapping).GetProperty("items").EnumerateArray()
                .ToDictionary(item => DesktopPortable.Read(item, "handle") ?? "",
                              item => item.GetProperty("usable").GetBoolean());
            if (flags.Count != 6 || !flags.TryGetValue("h-true", out var only) || !only)
                throw new IOException("preserved_usable_true_lost");
            var others = flags.Where(pair => pair.Key != "h-true").Select(pair => pair.Key).OrderBy(name => name);
            if (!others.SequenceEqual(new[]{"h-false","h-missing","h-null","h-number","h-string"}))
                throw new IOException("preserved_usable_items_lost");
            if (flags.Where(pair => pair.Key != "h-true").Any(pair => pair.Value))
                throw new IOException("preserved_usable_flag_not_strict_true");
            cases.Add("preserved_usable_flag_requires_json_true");
        }
        // 17. Preserved preview replies: token and labels only, never counters or a key slot.
        {
            var preview = DesktopPreserved.PreviewReply(PreservedPreviewResult());
            var result = DesktopPortable.Result(preview);
            var names = result.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!names.SequenceEqual(new[]{"effect","history","kind","name","preservation","records","source","taken_at","token"}))
                throw new IOException("preserved_preview_fields_not_whitelisted");
            if (DesktopPortable.Read(result, "kind") != "preserved"
                || DesktopPortable.Read(result, "token") != "synthetic-preserved-token"
                || DesktopPortable.ReadNumber(result, "records") != 12
                || DesktopPortable.Read(result, "name") != "aaaa.ccbackup"
                || DesktopPortable.Read(result, "taken_at") != "2026-11-07T10:11:12+00:00")
                throw new IOException("preserved_preview_reply_wrong");
            var source = DesktopPortable.Object(result, "source");
            var sourceNames = source.EnumerateObject().Select(property => property.Name).OrderBy(name => name);
            if (!sourceNames.SequenceEqual(new[]{"installation","project","role"}))
                throw new IOException("preserved_preview_source_not_whitelisted");
            if (DesktopPortable.Read(DesktopPortable.Object(result, "preservation"), "name") != PreservationName)
                throw new IOException("preserved_preview_preservation_missing");
            var raw = preview.GetRawText();
            foreach (var leaked in new[]{"counters", "ciphertexts", "keyref", "generation-2"})
                if (raw.Contains(leaked, StringComparison.Ordinal))
                    throw new IOException("preserved_preview_reply_leaked:" + leaked);
            cases.Add("preserved_preview_reply_hides_counters_and_key_slot");
        }
        // 18. The preserved operations use the portable budget and the same final handshake:
        //     a cancelled Windows prompt cancels instead of completing.
        {
            var list = DesktopPortable.Envelope(DesktopPreserved.ListOperation, new{after = ""});
            var preview = DesktopPortable.Envelope(DesktopPreserved.PreviewOperation, new{handle = "synthetic-handle"});
            if (list.GetProperty("action").GetString() != DesktopPortable.Action
                || preview.GetProperty("action").GetString() != DesktopPortable.Action)
                throw new IOException("preserved_envelope_action_changed");
            foreach (var request in new[]{list, preview})
            {
                if (StorageComponent.ReplySecondsForTest(request) != 240
                    || StorageComponent.CallMillisecondsForTest(request) != 245000)
                    throw new IOException("preserved_deadline_not_matched");
            }
            var operations = new List<string>();
            var flow = new PortableRestore((operation, _) =>
            {
                operations.Add(operation);
                return Task.FromResult(operation == DesktopPortable.BeginOperation
                    ? Begin("synthetic-challenge") : Ok(new{cancelled = true}));
            }, _ => Task.FromResult("Canceled"));
            var response = await flow.Run("synthetic-preserved-token", CancellationToken.None);
            if (!operations.SequenceEqual(new[]{"begin","cancel"}) || flow.CompleteSent
                || DesktopPortable.Read(response, "error") != "windows_verification")
                throw new IOException("preserved_windows_cancel_completed");
            if (DesktopPortable.Classify(response, flow.CompleteSent) != PortableSwitchOutcome.Refused)
                throw new IOException("preserved_windows_cancel_not_refused");
            cases.Add("preserved_windows_cancel_cancels_and_never_completes");
        }
        return cases.ToArray();
    }
}
