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


test('password uses the native contract and never returns password or raw failure data', async () => {
  let submitted;
  const state = new LoginState({passwordLogin: async args => {submitted=args;return {result:'0'};}});
  state.connected();
  const result=await state.password('10001','synthetic-password');
  assert.equal(submitted.passwordMd5, require('node:crypto').createHash('md5').update('synthetic-password').digest('hex'));
  assert.equal(submitted.step,0);assert.equal(submitted.uin,'10001');
  assert.deepEqual(result,{status:'authenticated',uin:'10001'});
  for(const code of ['140022008','140022010','140022011','wrong']) {
    const next=new LoginState({passwordLogin:async()=>({result:code,loginErrorInfo:{errMsg:'private-password',proofWaterUrl:'private-url'}})});
    next.connected();
    const value=await next.password('10001','synthetic-password');
    assert.deepEqual(value,{status:code==='wrong'?'failed':'verification_required'});
  }
});

test('cancel and expiry reject late password success and late QR callbacks', async () => {
  for(const cancel of [true,false]) {
    let finish,now=0;
    const state=new LoginState({passwordLogin:()=>new Promise(r=>finish=r)}, {clock:()=>now,lifetimeMs:10});
    state.connected();const pending=state.password('10001','synthetic-password');
    state.picture(picture);assert.equal(state.status().status,'password_pending');
    if(cancel)state.close();else now=11;
    finish({result:'0'});await pending;
    assert.equal(state.status().status,cancel?'cancelled':'expired');
  }
});

test('scanned QR waits for phone confirmation and expires or cancels cleanly',()=>{
 let now=0;const state=new LoginState({getQRCodePicture(){}},{clock:()=>now,lifetimeMs:10});
 state.connected();state.request();state.picture(picture);state.scanned();
 assert.equal(state.status().status,'qr_scanned');assert.ok(state.status().png_base64);
 state.picture(picture);assert.equal(state.status().status,'qr_scanned');
 now=11;assert.equal(state.status().status,'expired');state.succeeded({uin:'10001'});assert.equal(state.status().status,'expired');
});
