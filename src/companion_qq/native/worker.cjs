'use strict';
// SPDX-License-Identifier: MIT
// Private stdio control channel. Kernel stdout is not the protocol or a log sink.
const readline = require('node:readline'), crypto = require('node:crypto');
const {NativeSession} = require('./session.cjs');
let nonce = null, session = null, frozen = null, lastReadFailure = null, chain = Promise.resolve();
const readFailureReasons = new Map([
  ['No post-sync group head before deadline', 'group_head_timeout'],
  ['Group head snapshot unavailable', 'group_snapshot_unavailable'],
  ['Group contact snapshot unavailable', 'group_contact_unavailable'],
  ['Group contact initialization failed', 'group_contact_initialization_failed'],
  ['Incomplete group checkpoint identity', 'group_checkpoint_incomplete'],
  ['Checkpoint lookup failed', 'checkpoint_lookup_failed'],
  ['Checkpoint not uniquely resolved', 'checkpoint_not_unique'],
  ['Checkpoint author or time changed', 'checkpoint_identity_changed'],
  ['Message sync changed during history query', 'sync_generation_changed'],
  ['Message sync timeout', 'sync_timeout']
]);
const errors = new Set(['qq_login_required', 'qq_login_timeout', 'qq_target_unverified', 'qq_native_runtime_version',
  'qq_native_session_failed', 'qq_native_catalog_invalid', 'qq_native_not_ready', 'qq_native_protocol_invalid',
  'qq_native_send_state', 'qq_native_send_uncertain', 'qq_media_invalid', 'qq_media_state', 'qq_media_limit',
  'qq_media_path', 'qq_media_timeout', 'qq_media_download_failed', 'qq_media_integrity']);
const check = condition => {if (!condition) throw Error('qq_native_protocol_invalid');};
function reply(id, value) {process.stdout.write('\nQIBAN_NATIVE:' + nonce + ':' + JSON.stringify({id, ...value}) + '\n');}
async function dispatch(request) {
  check(request && Number.isSafeInteger(request.id) && request.id > 0 && typeof request.command === 'string');
  const {id, command, args} = request;
  if (command === 'open') {
    check(!session && typeof args?.nonce === 'string' && /^[a-f0-9]{64}$/.test(args.nonce));
    nonce = args.nonce;
  }
  try {
    let result;
    if (command === 'open') {
      session = new NativeSession(args.config); result = await session.open();
    } else {
      check(session !== null); session.guard();
      if (command === 'identity') result = session.catalog;
      else if (command === 'directory') {
        const {readDirectory} = require('./directory.cjs');
        result = await readDirectory(session.session, () => session.guard());
      }
      else if (command === 'prepare_file') result = session.sender.prepareFile(args.kind, args.target, args.file, args.name);
      else if (command === 'prepare_image') result = session.sender.prepareImage(args.kind, args.target, args.image, args.width, args.height, args.format);
      else if (command === 'prepare_voice') result = session.sender.prepareVoice(args.kind, args.target, args.audio, args.audio_ms);
      else if (command === 'prepare_text') result = session.sender.prepare(args.kind, args.target, args.text);
      else if (command === 'media_diagnostic') result = session.media.diagnostic;
      else if (command === 'send_diagnostic') result = session.sender.diagnostic;
      else if (command === 'submit_text') result = await session.sender.submit(args.token);
      else if (command === 'cancel_text') result = session.sender.cancel(args.token);
      else if (command === 'private_head') result = await session.privateHead(args.target, args.allow_empty === true);
      else if (command === 'group_head') result = await session.groupHead(args.target, args.allow_empty === true);
      else if (command === 'catalog_status') result = {group_updates: session.groupUpdates,
        activity: session.activity, history_queries: session.historyQueries, last_read_failure: lastReadFailure};
      else if (command === 'group_open' || command === 'private_open') {
        const kind = command === 'group_open' ? 'group' : 'private';
        const target = kind === 'group' ? args.group : args.target;
        const range = kind === 'group'
          ? await session.groupWindow(target, args.lower, args.upper || null, args.page_size ?? 100, args.since ?? null)
          : await session.privateWindow(target, args.lower, args.upper, args.page_size ?? 100, args.since ?? null);
        const upper = range.checkpoint;
        const windowId = crypto.createHash('sha256').update(JSON.stringify([session.catalog.uin, kind, target, args.lower, upper, ...(args.since == null ? [] : [args.since])])).digest('hex');
        frozen = {windowId, kind, target, lower: args.lower, upper, range, generation: session.gate.generation};
        result = {window_id: windowId, upper, count: range.messages.length, history_status: range.history_status};
      } else if (command === 'group_page' || command === 'private_page') {
        check(frozen && args.window_id === frozen.windowId && Number.isSafeInteger(args.page) && args.page >= 0);
        check(command === frozen.kind + '_page');
        const start = args.page * 100;
        check(start < frozen.range.messages.length || (start === 0 && frozen.range.messages.length === 0));
        result = {window_id: frozen.windowId, kind: frozen.kind, target: frozen.target, lower: frozen.lower,
          upper: frozen.upper, page: args.page, complete: start + 100 >= frozen.range.messages.length,
          messages: frozen.range.messages.slice(start, start + 100), history_status: frozen.range.history_status};
      } else if (command === 'download_attachment') {
        check(frozen && args.window_id === frozen.windowId && Number.isSafeInteger(args.index) && args.index >= 0);
        session.gate.assertCurrent(frozen.generation);
        const row = frozen.range.messages[args.index];
        check(row && String(row.senderUin) !== session.catalog.uin && row.senderUid !== session.catalog.uid);
        // A range can be reopened with the same stable bounds. Require the
        // exact locator just inspected, so shifted rows cannot select new media.
        check(args.source?.msg_id === row.msgId && args.source?.time === row.msgTime &&
          args.source?.sender_uid === row.senderUid &&
          args.source?.element_id === row.elements?.[args.position]?.elementId);
        check(frozen.kind === 'group' ? session.catalog.groups.includes(frozen.target)
          : session.catalog.friends[frozen.target] === row.peerUid);
          result = await session.media.download(row, args.position, args.max_bytes);
      } else if (command === 'close') {
        session.close(); reply(id, {ok: true, result: {closed: true}}); setImmediate(() => process.exit(0)); return;
      } else throw Error('qq_native_protocol_invalid');
    }
    reply(id, {ok: true, result});
  } catch (error) {
    const read = ['group_open', 'private_open', 'group_head', 'private_head'].includes(command);
    if (read) {
      lastReadFailure = {operation: command, reason: readFailureReasons.get(error.message) || 'unclassified',
        sync_phase: session.gate.phase, sync_generation: session.gate.generation};
    }
    const fallback = read ? 'qq_read_' + command + '_' + lastReadFailure.reason : 'qq_native_read_failed';
    reply(id, {ok: false, error: errors.has(error.message) ? error.message : fallback});
  }
}
readline.createInterface({input: process.stdin, crlfDelay: Infinity}).on('line', line => {
  chain = chain.then(() => {check(Buffer.byteLength(line) <= 14 * 1024 * 1024); const request = JSON.parse(line);
    check(Buffer.byteLength(line) <= 2 * 1024 * 1024 || ['prepare_image', 'prepare_file'].includes(request.command));
    check(Buffer.byteLength(line) <= 262144 || ['prepare_voice', 'prepare_image', 'prepare_file'].includes(request.command)); return dispatch(request);})
    .catch(() => {session?.close(); process.exit(2);});
}).on('close', () => {session?.close(); process.exit(0);});
