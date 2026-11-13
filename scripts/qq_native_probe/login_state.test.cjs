'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const {LoginState} = require('../../src/companion_qq/native/login_state.cjs');
const picture = {pngBase64QrcodeData: Buffer.from('89504e470d0a1a0a00', 'hex').toString('base64')};
test('login never requests QR until explicit action and does not refresh automatically', () => {
  let calls = 0;
  const state = new LoginState({getQRCodePicture() {calls++;}});
  state.connected(); assert.equal(calls, 0);
  state.request(); state.picture(picture); assert.equal(calls, 1);
  assert.equal(state.status().status, 'qr_ready');
  assert.throws(() => state.request(), /qq_login_state/);
  state.succeeded({uin: '10001'});
  assert.deepEqual(state.status(), {status: 'authenticated', uin: '10001'});
  state.fail(); assert.equal(state.status().status, 'authenticated');
});
test('cancelled and expired sessions discard QR and ignore late success', () => {
  for (const cancel of [true, false]) {
    let now = 0;
    const state = new LoginState({getQRCodePicture() {}}, {clock: () => now, lifetimeMs: 10});
    state.connected(); state.request(); state.picture(picture);
    if (cancel) state.close(); else now = 11;
    state.succeeded({uin: '10001'}); state.picture(picture);
    assert.deepEqual(state.status(), {status: cancel ? 'cancelled' : 'expired'});
  }
});
test('bad QR and invalid login identity fail without exposing raw payload', () => {
  for (const badPicture of [true, false]) {
    const state = new LoginState({getQRCodePicture() {}});
    state.connected(); state.request();
    if (badPicture) state.picture({pngBase64QrcodeData: 'private-invalid'});
    else state.succeeded({uin: 'private-invalid'});
    assert.deepEqual(state.status(), {status: 'failed'});
  }
});
