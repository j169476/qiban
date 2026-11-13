'use strict';
// SPDX-License-Identifier: MIT
// Deliberately return only display identity and quick-login availability, never raw login records.
function loginAccounts(value) {
  if (value?.result !== 0 || !Array.isArray(value.LocalLoginInfoList)) throw Error('qq_native_catalog_invalid');
  const seen = new Set();
  return value.LocalLoginInfoList.map(item => {
    if (typeof item?.uin !== 'string' || !/^[1-9][0-9]{4,19}$/.test(item.uin) || seen.has(item.uin) ||
        typeof item.uid !== 'string' || !/^u_.{1,126}$/.test(item.uid) || typeof item.nickName !== 'string' ||
        typeof item.isQuickLogin !== 'boolean') throw Error('qq_native_catalog_invalid');
    seen.add(item.uin);
    return {uin: item.uin, uid: item.uid, label: item.nickName.slice(0, 128), can_quick_login: item.isQuickLogin};
  });
}
module.exports = {loginAccounts};
