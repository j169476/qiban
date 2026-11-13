'use strict';
// SPDX-License-Identifier: MIT
const {readWindow} = require('./message_window.cjs');
function readPrivateWindow(options) {
  if (typeof options.peerUid !== 'string' || !options.peerUid.startsWith('u_')) throw Error('Private peer identity required');
  // Bounds must first be resolved inside the current cache. A lost checkpoint
  // is null, never an old cache's numeric sequence applied to the new cache.
  return readWindow({...options, chatType: 1, skipRecord: () => false});
}
module.exports = {readPrivateWindow};
