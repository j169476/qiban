'use strict';
// SPDX-License-Identifier: MIT
// Qiban-owned synchronization gate; no upstream runtime or implementation included.
class MessageSyncBarrier {
  constructor() { this.generation = 0; this.phase = 'waiting'; this.waiters = new Set(); }
  observe(event) {
    if (event === 'onNtMsgSyncStart') { this.generation++; this.phase = 'syncing'; }
    if (event === 'onNtMsgSyncEnd' && this.phase === 'syncing') {
      this.phase = 'ready';
      for (const waiter of [...this.waiters]) waiter.resolve(this.generation);
    }
  }
  invalidate() { this.generation++; this.phase = 'waiting'; }
  assertCurrent(generation) {
    if (this.phase !== 'ready' || generation !== this.generation) throw Error('Message sync changed during history query');
  }
  wait({ timeoutMs = 45000, signal } = {}) {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw Error('A finite positive sync deadline is required');
    if (signal?.aborted) return Promise.reject(signal.reason || Error('Message sync cancelled'));
    if (this.phase === 'ready') return Promise.resolve(this.generation);
    return new Promise((resolve, reject) => {
      let timer;
      const finish = (action, value) => {
        clearTimeout(timer); this.waiters.delete(waiter);
        signal?.removeEventListener('abort', abort); action(value);
      };
      const waiter = { resolve: value => finish(resolve, value) };
      const abort = () => finish(reject, signal.reason || Error('Message sync cancelled'));
      this.waiters.add(waiter);
      signal?.addEventListener('abort', abort, { once: true });
      timer = setTimeout(() => finish(reject, Error('Message sync timeout')), timeoutMs);
    });
  }
}
module.exports = { MessageSyncBarrier };
