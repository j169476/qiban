'use strict';
// SPDX-License-Identifier: MIT
// Field signatures only: NapCatQQ packages/napcat-core/types/{user,group}.ts.
// Independent directory reader; it never extends the session's reply catalog.
const {createListener} = require('./listener.cjs');
const LIMIT = 10000;
function entry(id, label) {
  id = String(id);
  if (!/^[1-9][0-9]{4,19}$/.test(id)) throw Error('qq_native_catalog_invalid');
  return {id, label: typeof label === 'string' ? [...label.trim()].slice(0, 128).join('') : ''};
}
function friends(categories) {
  if (!Array.isArray(categories) || categories.length > LIMIT) throw Error('qq_native_catalog_invalid');
  const rows = new Map(); let count = 0;
  for (const category of categories) {
    if (!Array.isArray(category?.buddyList)) throw Error('qq_native_catalog_invalid');
    for (const item of category.buddyList) {
      if (++count > LIMIT) throw Error('qq_native_catalog_invalid');
      // Pinned Windows kernel returns FriendV2.coreInfo; User is the legacy shape.
      const info = item?.coreInfo ?? item;
      const row = entry(item?.uin, info?.remark || info?.nick);
      row.nickname = entry(item?.uin, info?.nick).label;
      rows.set(row.id, row);
    }
  }
  return [...rows.values()];
}
function groups(mode, items) {
  if (!Array.isArray(items) || items.length > LIMIT) throw Error('qq_native_catalog_invalid');
  if (mode === 4 && items.length === 0 || mode === 2 || mode === 3) return null;
  if (mode !== 0 && mode !== 1) throw Error('qq_native_catalog_invalid');
  const rows = new Map();
  for (const item of items) {const row = entry(item?.groupCode, item?.groupName); rows.set(row.id, row);}
  return [...rows.values()];
}
async function readDirectory(session, guard, timeoutMs = 45000) {
  guard();
  const waits = [], cleanups = [];
  function read(register, request, parse) {
    return new Promise((resolve, reject) => {
      let done = false;
      const finish = (error, value) => {if (done) return; done = true; clearTimeout(timer); error ? reject(error) : resolve(value);};
      const timer = setTimeout(() => finish(Error('qq_native_catalog_invalid')), timeoutMs);
      cleanups.push(() => {done = true; clearTimeout(timer);});
      try {
        register((...args) => {
          if (done) return;
          try {guard(); const result = parse(...args); if (result !== null) finish(null, result);}
          catch (error) {finish(error);}
        });
        Promise.resolve(request()).catch(error => finish(error));
      } catch (error) {finish(error);}
    });
  }
  try {
    const buddy = session.getBuddyService(), group = session.getGroupService();
    waits.push(read(fn => buddy.addKernelBuddyListener(createListener('directory-buddy', {onBuddyListChange: fn})),
      () => buddy.getBuddyList(true), friends));
    waits.push(read(fn => group.addKernelGroupListener(createListener('directory-groups', {onGroupListUpdate: fn})),
      () => group.getGroupList(true), groups));
    const [private_targets, groupRows] = await Promise.all(waits);
    guard();
    const result = {private_targets, groups: groupRows};
    // Leaves room for JSON escaping and the rest of the 256 KiB management reply.
    if (Buffer.byteLength(JSON.stringify(result)) > 60000) throw Error('qq_native_catalog_invalid');
    return result;
  } finally {for (const cleanup of cleanups) cleanup();}
}
module.exports = {friends, groups, readDirectory};
