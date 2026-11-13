'use strict';
// SPDX-License-Identifier: MIT
const {readWindow} = require('./message_window.cjs');
const {isGroupSystemRecord} = require('./group_records.cjs');
function readGroupWindow(options) {
  if (!options.lower && !Number.isSafeInteger(options.since)) throw Error('Group lower sequence required');
  return readWindow({...options, chatType: 2, peerUid: options.group, skipRecord: isGroupSystemRecord});
}
module.exports = {readGroupWindow};
