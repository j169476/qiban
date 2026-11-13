'use strict';
// SPDX-License-Identifier: MIT
// Read a frozen, cache-local window using observed inclusive backwards pages.
// Reaching a known checkpoint is not proof of arbitrary server history completeness.
function identity(message) {
  if (typeof message?.msgId !== 'string' || !/^[1-9][0-9]*$/.test(message.msgId) ||
      typeof message.msgSeq !== 'string' || !/^[1-9][0-9]*$/.test(message.msgSeq)) throw Error('Invalid message identity');
  return {msgId: message.msgId, msgSeq: message.msgSeq};
}
async function readWindow({chatType, peerUid, skipRecord, lower, upper, query, assertCurrent, pageSize = 20, maxPages = 100, since = null}) {
  if (since !== null && (!Number.isSafeInteger(since) || since <= 0 || lower !== null))
    throw Error('Invalid authorization start');
  const absentLower = lower === null;
  const missingLower = absentLower || lower?.msgId === null;
  if (absentLower) {
    lower = {msgId: null, msgSeq: null};
  } else if (missingLower) {
    if (typeof lower.msgSeq !== 'string' || !/^[1-9][0-9]*$/.test(lower.msgSeq)) throw Error('Invalid lower sequence');
    lower = {msgId: null, msgSeq: lower.msgSeq};
  } else lower = identity(lower);
  upper = identity(upper);
  if (!Number.isInteger(pageSize) || pageSize < 2 || pageSize > 100 || !Number.isInteger(maxPages) || maxPages < 1) throw Error('Invalid page limits');
  const low = absentLower ? -1n : BigInt(lower.msgSeq), high = BigInt(upper.msgSeq);
  if (low > high || (low === high && !missingLower && lower.msgId !== upper.msgId)) throw Error('Conflicting window bounds');
  assertCurrent();
  let cursor = upper;
  const seen = new Map(), visitedCursors = new Set();
  let pages = 0;
  function finish(reachedCheckpoint, reason) {
    assertCurrent();
    const reasons = [...new Set([...(missingLower && since === null ? ['checkpoint_unavailable'] : []), ...(reason ? [reason] : [])])];
    const messages = [...seen.values()].map(x => x.message).sort((a, b) => BigInt(a.msgSeq) < BigInt(b.msgSeq) ? -1 : 1);
    return {messages, pages, lower, upper, reachedCheckpoint: reachedCheckpoint && !missingLower,
      history_status: {status: reasons.length ? 'incomplete' : 'complete', reasons}};
  }
  if (low === high) return finish(true);
  for (let page = 1; page <= maxPages; page++) {
    assertCurrent();
    if (visitedCursors.has(cursor.msgId)) throw Error('History pagination repeated a cursor');
    visitedCursors.add(cursor.msgId);
    const response = await query(cursor.msgId, pageSize);
    assertCurrent();
    pages = page;
    const queryFailed = response?.result !== 0;
    if (queryFailed && response?.msgList === undefined) return finish(false, 'history_query_failed');
    if (!Array.isArray(response.msgList)) throw Error('Invalid history response');
    if (response.msgList.length > pageSize) throw Error('History page exceeds requested bound');
    const messages = response.msgList;
    if (!messages.length) return finish(false, queryFailed ? 'history_query_failed' : 'history_unavailable');
    if (!messages.some(m => m.msgId === cursor.msgId && m.msgSeq === cursor.msgSeq)) throw Error('History page missing inclusive anchor');
    let oldest = cursor, reached = false;
    for (const message of messages) {
      const key = identity(message);
      if (message.chatType !== chatType || message.peerUid !== peerUid) throw Error('History changed conversation');
      const seq = BigInt(key.msgSeq);
      if (seq > high) continue; // New arrivals belong to the next window.
      if (seq < BigInt(oldest.msgSeq) || (seq === BigInt(oldest.msgSeq) && oldest.msgId === cursor.msgId && key.msgId !== cursor.msgId)) oldest = key;
      if (since !== null) {
        if (!/^[1-9][0-9]*$/.test(String(message.msgTime)) || !Number.isSafeInteger(Number(message.msgTime)))
          throw Error('Invalid message timestamp');
        if (Number(message.msgTime) < since) {reached = true; continue;}
      }
      if (skipRecord(message)) continue; // Traverse tips but never use them as conversation checkpoints.
      if (seq === low) {
        if (!missingLower && key.msgId !== lower.msgId) throw Error('Checkpoint identity changed');
        reached = true;
      }
      if (seq <= low) continue;
      const fingerprint = JSON.stringify([message.senderUin, message.senderUid, message.msgTime, message.msgRandom, message.clientSeq, message.elements]);
      const prior = seen.get(key.msgSeq);
      if (prior && (prior.message.msgId !== key.msgId || prior.fingerprint !== fingerprint)) throw Error('History source changed while paging');
      seen.set(key.msgSeq, {message: structuredClone(message), fingerprint});
    }
    // QQ can return usable cache rows together with a failed history query.
    // Validate all rows as usual, retain them, but do not claim completion.
    if (queryFailed) return finish(false, 'history_query_failed');
    if (reached) {
      assertCurrent();
      return finish(true);
    }
    if (BigInt(oldest.msgSeq) < low) return finish(false, 'checkpoint_unavailable');
    if (oldest.msgId === cursor.msgId) return finish(false, 'history_read_stalled');
    cursor = oldest;
  }
  return finish(false, 'history_page_limit');
}
module.exports = {readWindow};
