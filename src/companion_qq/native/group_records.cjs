'use strict';
// SPDX-License-Identifier: MIT
// Observed native system-tip rows may share a sequence with a conversation message.
function isGroupSystemRecord(message) {
  return message?.chatType === 2 && message.msgType === 5 && String(message.senderUin) === '0' &&
    message.senderUid === '' && Array.isArray(message.elements) && message.elements.length > 0 &&
    message.elements.every(element => element.elementType === 8);
}
module.exports = {isGroupSystemRecord};
