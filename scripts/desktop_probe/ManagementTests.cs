// SPDX-License-Identifier: MIT
using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;

namespace Qiban.DesktopProbe;

internal static class ManagementTests
{
    internal static async Task<int> Impostor(string pipeName)
    {
        using var pipe=new NamedPipeClientStream(".",pipeName,PipeDirection.InOut,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
        using var deadline=new CancellationTokenSource(5000);
        await pipe.ConnectAsync(deadline.Token);
        try
        {
            using var writer=new StreamWriter(pipe,leaveOpen:true){AutoFlush=true};
            using var reader=new StreamReader(pipe,leaveOpen:true);
            await writer.WriteLineAsync("{\"action\":\"management\",\"operation\":\"session.status\",\"arguments\":{},\"request_id\":\"01234567890123456789012345678901\"}");
            return await reader.ReadLineAsync(deadline.Token) is null?0:1;
        }
        catch(IOException){return 0;} // Connected, then rejected before returning any management data.
    }

    internal static async Task<int> Run(string root,string application,string evidence)
    {
        Directory.CreateDirectory(evidence);
        string pipe="qiban-management-test-"+Guid.NewGuid().ToString("N");
        using var group=InstallationComponent.Group(root,application,pipe);
        var cases=new List<string>();bool passed=false;
        async Task<JsonElement> Call(string operation,object arguments,string? id=null)
        {
            var request=JsonSerializer.SerializeToElement(new{action="management",operation,arguments,request_id=id??Guid.NewGuid().ToString("N")});
            File.AppendAllText(Path.Combine(evidence,"operations.jsonl"),JsonSerializer.Serialize(new{operation,stage="start"})+"\n");
            var response=await StorageComponent.Call(pipe+"-storage",group.Process!,request);
            File.AppendAllText(Path.Combine(evidence,"operations.jsonl"),JsonSerializer.Serialize(new{operation,stage="complete",
                error=response.TryGetProperty("error",out var code)?code.GetString():null})+"\n");
            return response;
        }
        static JsonElement Ok(JsonElement response)
        {
            if(!response.TryGetProperty("ok",out _))throw new IOException("invalid_management_response");
            if(!response.GetProperty("ok").GetBoolean())throw new IOException(response.GetProperty("error").GetString());
            return response.GetProperty("result");
        }
        static void Denied(JsonElement response,string error)
        {
            if(response.GetProperty("ok").GetBoolean()||response.GetProperty("error").GetString()!=error)throw new IOException("wrong_management_rejection");
        }
        try
        {
            cases.AddRange(await RecoveryTests.Run());
            await group.StartAsync();if(group.State!="ready")throw new IOException(group.Detail);
            var status=Ok(await Call("session.status",new{}));
            if(status.GetProperty("locked").GetBoolean()||status.GetProperty("lock_enabled").GetBoolean())throw new IOException("default_not_passwordless");
            cases.Add("windows_owner_default_without_password");
            var runtimeBefore=Ok(await Call("runtime.status",new{}));
            if(runtimeBefore.GetProperty("role").GetProperty("platform").GetString()!="qiban-role")throw new IOException("role_identity_missing");
            foreach(var platform in runtimeBefore.GetProperty("platforms").EnumerateArray())
                if(platform.GetProperty("autorun").GetBoolean()||platform.GetProperty("available").GetBoolean()!=(platform.GetProperty("platform").GetString()=="local"))throw new IOException("fresh_platform_started");
            Denied(await Call("runtime.control",new{platform="qq",action="start"}),"platform_unavailable");
            if(Ok(await Call("notifications.list",new{})).GetProperty("unread").GetInt32()!=0)throw new IOException("unexpected_fresh_notice");
            cases.Add("own_role_identity_fresh_local_room_available_platforms_stopped");
            var taskSession=status.GetProperty("room_session").GetString();
            var taskList=Ok(await Call("tasks.list",new{session=taskSession}));
            if(taskList.GetProperty("items").GetArrayLength()!=0||taskList.GetProperty("capabilities").GetProperty("video").GetProperty("available").GetBoolean())throw new IOException("manual_tasks_fresh_state");
            Denied(await Call("tasks.list",new{session="forged"}),"desktop_locked");
            Denied(await Call("tasks.submit",new{session=taskSession,kind="web",arguments=new{prompt="synthetic unsupported request"},nonce=Guid.NewGuid().ToString("N")}),"model_capability_unavailable");
            cases.Add("native_manual_task_session_and_capability_gate_no_model_call");
            var original=Ok(await Call("config.read",new{kind="preferences"}));
            string id=Guid.NewGuid().ToString("N");
            var args=new{kind="preferences",expected=original.GetProperty("version").GetString(),changes=new{topics=new[]{"桌面管理验证"}}};
            var written=Ok(await Call("config.patch",args,id));
            var repeated=Ok(await Call("config.patch",args,id));
            if(written.GetRawText()!=repeated.GetRawText())throw new IOException("request_not_deduplicated");
            Denied(await Call("config.patch",new{kind="preferences",expected=args.expected,changes=new{topics=new[]{"重复标识"}}},id),"request_id_conflict");
            Denied(await Call("config.patch",args),"edit_conflict");
            cases.Add("shared_config_version_and_request_deduplication");
            var recordTarget=new{kind="memories",identifier="desktop-native-record"};
            var recordArgs=new{recordTarget.kind,recordTarget.identifier,expected=(string?)null,
                value=new{schema_version=1,id=recordTarget.identifier,kind="user_preference",content="桌面原生资料验证",
                    confidence="observed",tags=Array.Empty<string>(),locked=false,
                    source=new{type="user_instruction",reference="native-test",recorded_at="2026-10-30T00:00:00Z"}}};
            string recordId=Guid.NewGuid().ToString("N");
            var record=Ok(await Call("records.correct",recordArgs,recordId));
            var detail=Ok(await Call("records.read",recordTarget));
            if(detail.GetProperty("data").GetProperty("content").GetString()!=recordArgs.value.content)throw new IOException("record_read_mismatch");
            var listing=Ok(await Call("records.list",new{kind="memories",after="",limit=10}));
            if(listing.GetProperty("records").GetArrayLength()!=1)throw new IOException("record_list_mismatch");
            const string unicodeText="中文、数字456、表情🙂、生僻字𠮷";
            var unicodeArgs=recordArgs with {expected=record.GetProperty("version").GetString(),
                value=recordArgs.value with {content=unicodeText}};
            string literalRequest=JsonSerializer.Serialize(new{action="management",operation="records.correct",
                arguments=unicodeArgs,request_id=Guid.NewGuid().ToString("N")})
                .Replace("\\uD83D\\uDE42","🙂").Replace("\\uD842\\uDFB7","𠮷");
            if(!literalRequest.Contains("🙂") || !literalRequest.Contains("𠮷"))throw new IOException("unicode_fixture_not_literal");
            JsonElement unicodeRecord;
            using(var literalDocument=JsonDocument.Parse(literalRequest))
                unicodeRecord=Ok(await StorageComponent.Call(pipe+"-storage",group.Process!,literalDocument.RootElement));
            detail=Ok(await Call("records.read",recordTarget));
            if(detail.GetProperty("data").GetProperty("content").GetString()!=unicodeText)throw new IOException("literal_unicode_roundtrip_mismatch");
            cases.Add("literal_webview_unicode_survives_backend_transport");
            string yaml=Ok(await Call("records.export",recordTarget)).GetString()!;
            var preview=Ok(await Call("records.import_preview",new{recordTarget.kind,recordTarget.identifier,text=yaml,expected_version=unicodeRecord.GetProperty("version").GetString()}));
            if(preview.GetProperty("written").GetBoolean())throw new IOException("preview_wrote_record");
            cases.Add("native_shared_record_read_write_list_export_and_preview");
            var spoof=JsonSerializer.SerializeToElement(new{action="management",operation="config.read",arguments=new{kind="persona"},request_id=Guid.NewGuid().ToString("N"),administrator=true});
            Denied(await StorageComponent.Call(pipe+"-storage",group.Process!,spoof),"management_request");
            cases.Add("caller_admin_flag_rejected");
            foreach(string suffix in new[]{"-storage","-database","-install"})
            {
                using var impostor=OwnedProcess.Start(Environment.ProcessPath!,"--management-impostor",pipe+suffix);
                await impostor.Process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8));
                if(impostor.Process.ExitCode!=0)throw new IOException("impostor_not_rejected"+suffix+":"+impostor.Process.ExitCode);
            }
            await group.CheckAsync();if(group.State!="ready")throw new IOException("impostor_disrupted_server");
            cases.Add("different_process_cannot_use_management_pipe");
            const string password="Synthetic-Desktop-Lock-2026!";
            var roomSession=Ok(await Call("lock.enable",new{password})).GetProperty("room_session").GetString();
            Ok(await Call("room.history",new{session=roomSession,before=""}));
            Ok(await Call("session.lock",new{}));
            Denied(await Call("room.history",new{session=roomSession,before=""}),"desktop_locked");
            Denied(await Call("room.post",new{session=roomSession,text="locked",nonce=Guid.NewGuid().ToString("N")}),"desktop_locked");
            Denied(await Call("config.read",new{kind="persona"}),"desktop_locked");
            Denied(await Call("records.read",recordTarget),"desktop_locked");
            Denied(await Call("session.unlock",new{password="Incorrect-Test-Password"}),"desktop_unlock_denied");
            await group.StopAsync();await group.StartAsync();if(group.State!="ready")throw new IOException(group.Detail);
            status=Ok(await Call("session.status",new{}));
            if(!status.GetProperty("locked").GetBoolean())throw new IOException("restart_bypassed_lock");
            Denied(await Call("config.patch",args,id),"desktop_locked");
            cases.Add("optional_lock_blocks_reads_writes_and_restart_bypass");
            var newRoom=Ok(await Call("session.unlock",new{password})).GetProperty("room_session").GetString();
            Denied(await Call("room.history",new{session=roomSession,before=""}),"desktop_locked");
            Ok(await Call("room.history",new{session=newRoom,before=""}));
            cases.Add("room_locked_stale_request_refused_and_owner_history_reopened");
            repeated=Ok(await Call("config.patch",args,id));
            if(repeated.GetRawText()!=written.GetRawText())throw new IOException("restart_lost_receipt");
            if(Ok(await Call("records.correct",recordArgs,recordId)).GetRawText()!=record.GetRawText())throw new IOException("restart_lost_record_receipt");
            var deletion=Ok(await Call("records.delete_preview",recordTarget));
            var deleteArgs=new{token=deletion.GetProperty("token").GetString()};
            string deleteId=Guid.NewGuid().ToString("N");
            var deleted=Ok(await Call("records.delete",deleteArgs,deleteId));
            if(!deleted.GetProperty("deleted").GetBoolean() || Ok(await Call("records.delete",deleteArgs,deleteId)).GetRawText()!=deleted.GetRawText())throw new IOException("delete_receipt_mismatch");
            Denied(await Call("records.delete",deleteArgs),"delete_denied");
            cases.Add("native_record_restart_receipt_and_confirmed_delete");
            Denied(await Call("lock.disable",new{password="Incorrect-Test-Password"}),"desktop_unlock_denied");
            Ok(await Call("lock.disable",new{password}));
            cases.Add("password_verification_and_persistent_request_receipt");
            await group.StopAsync();await group.StartAsync();if(group.State!="ready")throw new IOException(group.Detail);
            status=Ok(await Call("session.status",new{}));
            if(status.GetProperty("locked").GetBoolean())throw new IOException("disabled_lock_still_requires_login");
            cases.Add("explicit_disable_restores_passwordless_restart");
            var runtimeAfter=Ok(await Call("runtime.status",new{}));
            if(runtimeAfter.GetProperty("role").GetRawText()!=runtimeBefore.GetProperty("role").GetRawText()
                || runtimeAfter.GetProperty("application_session").GetString()!=runtimeBefore.GetProperty("application_session").GetString()
                || runtimeAfter.GetProperty("runtime_instance").GetString()==runtimeBefore.GetProperty("runtime_instance").GetString())throw new IOException("runtime_restart_identity_mismatch");
            cases.Add("backend_restart_keeps_app_session_and_role_with_new_runtime");
            var backup=Ok(await Call("backups.create",new{}));
            var beforeRestore=Ok(await Call("config.read",new{kind="preferences"}));
            Ok(await Call("config.patch",new{kind="preferences",changes=new{topics=new[]{"恢复前的新资料"}},expected=beforeRestore.GetProperty("version").GetString()}));
            Ok(await Call("lock.enable",new{password}));
            var restorePreview=Ok(await Call("backups.preview",new{name=backup.GetProperty("name").GetString()}));
            async Task<JsonElement> RestoreExchange(string operation,object arguments)=>await StorageComponent.Call(pipe+"-storage",group.Process!,
                JsonSerializer.SerializeToElement(new{action="desktop-restore",operation,arguments}));
            var cancelRestore=new BackupRestore(RestoreExchange,_=>Task.FromResult("Canceled"));
            Denied(await cancelRestore.Run(restorePreview.GetProperty("token").GetString()!,CancellationToken.None),"windows_verification");
            if(Ok(await Call("config.read",new{kind="preferences"})).GetProperty("data").GetProperty("topics")[0].GetString()!="恢复前的新资料")throw new IOException("cancel_changed_records");
            restorePreview=Ok(await Call("backups.preview",new{name=backup.GetProperty("name").GetString()}));
            var verifiedRestore=new BackupRestore(RestoreExchange,_=>Task.FromResult("Verified"));
            var restoreResult=Ok(await verifiedRestore.Run(restorePreview.GetProperty("token").GetString()!,CancellationToken.None));
            if(restoreResult.GetProperty("last_restore").GetProperty("status").GetString()!="complete")throw new IOException("restore_incomplete");
            if(Ok(await Call("config.read",new{kind="preferences"})).GetRawText()!=beforeRestore.GetRawText())throw new IOException("restore_content_mismatch");
            if(!Ok(await Call("session.status",new{})).GetProperty("lock_enabled").GetBoolean())throw new IOException("restore_reverted_current_lock");
            await group.StopAsync();await group.StartAsync();if(group.State!="ready")throw new IOException(group.Detail);
            if(!Ok(await Call("session.status",new{})).GetProperty("locked").GetBoolean())throw new IOException("restore_restart_bypassed_lock");
            Ok(await Call("session.unlock",new{password}));
            if(Ok(await Call("config.read",new{kind="preferences"})).GetRawText()!=beforeRestore.GetRawText())throw new IOException("restore_restart_content_mismatch");
            cases.Add("native_backup_restore_mock_verified_cancel_current_lock_and_restart");
            var baselinePreview=Ok(await Call("baseline.preview",new{}));
            var personView=Ok(await Call("relationship.read",new{}));
            if(personView.GetProperty("selected").GetArrayLength()!=0)throw new IOException("unexpected_person_selection");
            var actorPage=Ok(await Call("relationship.accounts",new{after=""}));
            if(actorPage.GetProperty("accounts").ValueKind!=JsonValueKind.Array)throw new IOException("invalid_person_accounts");
            Denied(await Call("relationship.person",new{anchor="missing-synthetic-account"}),"unknown_actor");
            cases.Add("native_person_selector_reads_and_rejects_missing_actor");
            var publishArgs=new{expected=new{pins=baselinePreview.GetProperty("pins"),foundation_count=baselinePreview.GetProperty("foundation_count")}};
            string publishId=Guid.NewGuid().ToString("N");
            var published=Ok(await Call("baseline.publish",publishArgs,publishId));
            if(!published.GetProperty("shared").GetBoolean() || published.GetProperty("role_started").GetBoolean()
                || Ok(await Call("baseline.publish",publishArgs,publishId)).GetRawText()!=published.GetRawText())throw new IOException("baseline_receipt_mismatch");
            var persona=Ok(await Call("config.read",new{kind="persona"}));
            Ok(await Call("foundation.add",new{identifier="native-foundation",content="合成共用设定🙂",expected=persona.GetProperty("version").GetString()}));
            if(Ok(await Call("records.read",new{kind="memories",identifier="native-foundation"})).GetProperty("data").GetProperty("content").GetString()!="合成共用设定🙂")throw new IOException("foundation_unicode_mismatch");
            Denied(await Call("records.delete_preview",new{kind="memories",identifier="native-foundation"}),"baseline_locked");
            persona=Ok(await Call("config.read",new{kind="persona"}));
            Ok(await Call("config.patch",new{kind="persona",expected=persona.GetProperty("version").GetString(),changes=new{foundation_memories=Array.Empty<string>()}}));
            if(Ok(await Call("records.read",new{kind="memories",identifier="native-foundation"})).GetProperty("policy").GetProperty("valid").GetBoolean())throw new IOException("removed_foundation_still_accessible");
            cases.Add("native_baseline_publication_receipt_add_unicode_and_remove_without_delete");
            var inspection=Ok(await Call("records.explain",new{kind="memories",identifier="native-foundation"}));
            if(!inspection.GetProperty("issues").EnumerateArray().Any(issue=>issue.GetProperty("reason").GetString()=="revoked"))throw new IOException("source_inspection_missing_revocation");
            if(Ok(await Call("records.read",new{kind="memories",identifier="native-foundation"})).GetProperty("data").GetProperty("content").GetString()!="合成共用设定🙂")throw new IOException("source_inspection_changed_content");
            cases.Add("native_readonly_source_inspection_reports_revoked_foundation");
            var importTarget=new{kind="memories",identifier="native-import-161"};
            Ok(await Call("records.correct",recordArgs with {identifier=importTarget.identifier,
                value=recordArgs.value with {id=importTarget.identifier}}));
            var exported=Ok(await Call("records.advanced_export",importTarget));
            string importVersion=exported.GetProperty("version").GetString()!;
            var importPreview=Ok(await Call("records.advanced_preview",new{importTarget.kind,importTarget.identifier,
                text=exported.GetProperty("text").GetString(),expected_version=importVersion}));
            if(importPreview.GetProperty("written").GetBoolean() || importPreview.GetProperty("changed").GetBoolean())throw new IOException("advanced_roundtrip_changed");
            string cancelledToken=importPreview.GetProperty("token").GetString()!;
            Ok(await Call("records.advanced_cancel",new{token=cancelledToken}));
            Denied(await Call("records.advanced_commit",new{token=cancelledToken}),"record_import");
            var importData=System.Text.Json.Nodes.JsonNode.Parse(Ok(await Call("records.read",importTarget)).GetProperty("data").GetRawText())!;
            importData["content"]="高级导入中文123🙂𠮷";
            string importText=JsonSerializer.Serialize(new{format="qiban-record-1",kind="memories",record_version=importVersion,
                policy_version=exported.GetProperty("policy_version").GetString(),data=importData,references=Array.Empty<object>()});
            importPreview=Ok(await Call("records.advanced_preview",new{importTarget.kind,importTarget.identifier,text=importText,expected_version=importVersion}));
            string importToken=importPreview.GetProperty("token").GetString()!;
            var diff=Ok(await Call("records.advanced_changes",new{token=importToken,offset=0}));
            if(!diff.GetProperty("text").GetString()!.Contains("高级导入中文123🙂𠮷"))throw new IOException("advanced_diff_unicode");
            string importRequest=Guid.NewGuid().ToString("N");
            var imported=Ok(await Call("records.advanced_commit",new{token=importToken},importRequest));
            if(Ok(await Call("records.advanced_commit",new{token=importToken},importRequest)).GetRawText()!=imported.GetRawText())throw new IOException("advanced_receipt_mismatch");
            if(Ok(await Call("records.read",importTarget)).GetProperty("data").GetProperty("content").GetString()!="高级导入中文123🙂𠮷")throw new IOException("advanced_import_read_mismatch");
            cases.Add("native_advanced_export_cancel_full_diff_unicode_atomic_import_and_receipt");
            exported=Ok(await Call("records.advanced_export",importTarget));
            string manyEmoji=string.Concat(Enumerable.Repeat("🙂",5800));
            string largeYaml=exported.GetProperty("text").GetString()!.Replace("高级导入中文123🙂𠮷",manyEmoji);
            if(!largeYaml.Contains(manyEmoji) || System.Text.Encoding.UTF8.GetByteCount(largeYaml)>24000)throw new IOException("large_unicode_fixture_invalid");
            var largeArgs=new{importTarget.kind,importTarget.identifier,text=largeYaml,expected_version=exported.GetProperty("version").GetString()};
            if(JsonSerializer.Serialize(new{action="management",operation="records.advanced_preview",arguments=largeArgs,request_id=Guid.NewGuid().ToString("N")}).Length<=65536)throw new IOException("large_unicode_fixture_did_not_cross_old_limit");
            importPreview=Ok(await Call("records.advanced_preview",largeArgs));
            importToken=importPreview.GetProperty("token").GetString()!;
            diff=Ok(await Call("records.advanced_changes",new{token=importToken,offset=0}));
            if(!diff.GetProperty("text").GetString()!.Contains(manyEmoji))throw new IOException("large_unicode_diff_truncated");
            Ok(await Call("records.advanced_cancel",new{token=importToken}));
            cases.Add("native_under_24kb_emoji_yaml_crosses_old_64k_pipe_limit_without_truncation");
            passed=true;
        }
        finally
        {
            await group.StopAsync();
            File.WriteAllText(Path.Combine(evidence,"management-result.json"),JsonSerializer.Serialize(new{passed,cases}));
        }
        return passed?0:1;
    }
}
