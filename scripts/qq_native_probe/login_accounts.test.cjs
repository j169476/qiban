'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const {loginAccounts} = require('../../src/companion_qq/native/login_accounts.cjs');
const item = {uin:'10001',uid:'u_self',nickName:'测试🙂',isQuickLogin:true,secret:'must-not-leave',facePath:'private'};
test('login account catalog exposes only verified identity and availability', () => {
  assert.deepEqual(loginAccounts({result:0,LocalLoginInfoList:[item]}),
    [{uin:'10001',uid:'u_self',label:'测试🙂',can_quick_login:true}]);
  assert.deepEqual(loginAccounts({result:0,LocalLoginInfoList:[]}),[]);
});
test('ambiguous or missing login identity is not silently selected', () => {
  for(const value of [{result:1,LocalLoginInfoList:[item]}, {result:0},
    {result:0,LocalLoginInfoList:[item,item]}, {result:0,LocalLoginInfoList:[{...item,uid:''}]},
    {result:0,LocalLoginInfoList:[{...item,isQuickLogin:undefined}]}]) {
    assert.throws(()=>loginAccounts(value),/qq_native_catalog_invalid/);
  }
});
