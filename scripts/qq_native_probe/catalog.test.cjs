'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const {updateGroups} = require('../../src/companion_qq/native/catalog.cjs');
test('snapshot, sparse modification and empty observed notification preserve other targets', () => {
  const allowed = ['10001', '10002'];
  let groups = updateGroups([], 0, [{groupCode: '10001'}, {groupCode: '10002'}, {groupCode: '99999'}], allowed);
  groups = updateGroups(groups, 4, [], allowed);
  groups = updateGroups(groups, 2, [{groupCode: '10001'}], allowed);
  groups = updateGroups(groups, 4, [], allowed);
  assert.deepEqual(groups, allowed);
});
test('remove and new full snapshot revoke membership; unknown nonempty mode is not guessed', () => {
  const allowed = ['10001', '10002'];
  assert.deepEqual(updateGroups(allowed, 3, [{groupCode: '10001'}], allowed), ['10002']);
  assert.deepEqual(updateGroups(allowed, 0, [], allowed), []);
  assert.throws(() => updateGroups(allowed, 4, [{groupCode: '10001'}], allowed));
  assert.throws(() => updateGroups(allowed, 9, [], allowed));
});
test('latest buddy list removes absent targets and rejects conflicting identities',()=>{
  const {currentFriends} = require('../../src/companion_qq/native/catalog.cjs');
  const peers=['10001','10002'];
  const one={uin:'10001',uid:'u_one'},two={uin:'10002',uid:'u_two'};
  assert.deepEqual(currentFriends([{buddyList:[one,two]}],peers),{'10001':'u_one','10002':'u_two'});
  assert.deepEqual(currentFriends([{buddyList:[two]}],peers),{'10002':'u_two'});
  assert.throws(()=>currentFriends([{buddyList:[two,{...two,uid:'u_other'}]}],peers));
});
