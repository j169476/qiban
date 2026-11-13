'use strict';
// SPDX-License-Identifier: MIT
// Protocol enum facts: https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/types/group.ts
// Independent implementation; no upstream bridge implementation is included.
function updateGroups(current, mode, entries, allowed) {
  if (!Array.isArray(entries)) throw Error('qq_native_catalog_invalid');
  // The pinned 9.9.28 kernel also emits empty mode-4 notifications between
  // snapshots/updates. They contain no membership assertions; preserve state.
  if (mode === 4 && entries.length === 0) return [...current];
  if (![0, 1, 2, 3].includes(mode)) throw Error('qq_native_catalog_invalid');
  const ids = entries.map(entry => String(entry?.groupCode));
  if (ids.some(id => !/^[1-9][0-9]{4,19}$/.test(id))) throw Error('qq_native_catalog_invalid');
  const permitted = allowed === null ? null : new Set(allowed);
  const matched = [...new Set(ids.filter(id => !permitted || permitted.has(id)))];
  if (mode === 0 || mode === 1) return matched;
  if (mode === 2) return [...new Set([...current, ...matched])];
  return current.filter(group => !matched.includes(group));
}
function currentFriends(categories, allowed) {
  if (!Array.isArray(categories)) throw Error('qq_native_catalog_invalid');
  const selected = allowed === null ? null : new Set(allowed), current = {};
  for (const category of categories) {
    if (!Array.isArray(category?.buddyList)) throw Error('qq_native_catalog_invalid');
    for (const entry of category.buddyList) {
      const id = String(entry?.uin);
      if (selected && !selected.has(id)) continue;
      if (!/^[1-9][0-9]{4,19}$/.test(id)) throw Error('qq_native_catalog_invalid');
      const uid = entry?.uid;
      if (typeof uid !== 'string' || !uid.startsWith('u_') || (current[id] && current[id] !== uid))
        throw Error('qq_native_catalog_invalid');
      current[id] = uid;
    }
  }
  return current;
}
module.exports = {updateGroups, currentFriends};
