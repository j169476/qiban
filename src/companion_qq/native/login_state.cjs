'use strict';
// SPDX-License-Identifier: MIT
// Only explicit user actions request a QR; callbacks cannot restart a login.
class LoginState {
  constructor(login, {clock = () => Date.now(), lifetimeMs = 300000, pictureTimeoutMs = 30000} = {}) {
    this.login = login; this.clock = clock; this.lifetimeMs = lifetimeMs; this.pictureTimeoutMs = pictureTimeoutMs;
    this.value = {status: 'connecting'}; this.deadline = null; this.closed = false;
  }
  connected() {if (!this.closed && this.value.status === 'connecting') this.value = {status: 'ready'};}
  active() {return !this.closed && ['requesting_qr', 'qr_ready', 'qr_scanned', 'password_pending'].includes(this.value.status);}
  scanned() {
    if (this.status().status === 'qr_ready') this.value = {...this.value, status: 'qr_scanned'};
  }
  status() {
    if (this.active() && this.clock() >= this.deadline)
      this.fail(this.value.status === 'requesting_qr' ? 'picture_timeout' : 'expired');
    return {...this.value};
  }
  request() {
    if (this.closed || this.status().status !== 'ready') throw Error('qq_login_state');
    this.value = {status: 'requesting_qr'}; this.deadline = this.clock() + this.pictureTimeoutMs;
    try {
      Promise.resolve(this.login.getQRCodePicture()).catch(() => this.fail('failed'));
    } catch {this.fail('failed');}
    return this.status();
  }
  async password(uin, password) {
    if (this.closed || this.status().status !== 'ready' || !/^[1-9][0-9]{4,19}$/.test(uin) ||
        typeof password !== 'string' || password.length < 1 || password.length > 128 || password.includes('\0'))
      throw Error('qq_login_state');
    this.value = {status: 'password_pending'}; this.deadline = this.clock() + this.lifetimeMs;
    try {
      const passwordMd5 = require('node:crypto').createHash('md5').update(password, 'utf8').digest('hex');
      password = '';
      const reply = await this.login.passwordLogin({uin, passwordMd5, step: 0,
        newDeviceLoginSig: new Uint8Array(), proofWaterSig: new Uint8Array(),
        proofWaterRand: new Uint8Array(), proofWaterSid: new Uint8Array(), unusualDeviceCheckSig: new Uint8Array()});
      if (!this.active() || this.status().status === 'expired') return this.status();
      if (String(reply?.result) === '0') this.succeeded({uin});
      else if (['140022008', '140022010', '140022011'].includes(String(reply?.result)))
        this.value = {status: 'verification_required'};
      else this.fail();
    } catch {this.fail();}
    finally {password = '';}
    return this.status();
  }
  picture(event) {
    if (!['requesting_qr', 'qr_ready'].includes(this.status().status)) return;
    const encoded = event?.pngBase64QrcodeData;
    if (typeof encoded !== 'string' || encoded.length > 350000) {this.fail('failed'); return;}
    const png = Buffer.from(encoded.replace(/^data:image\/png;base64,/, ''), 'base64');
    if (png.length > 262144 || png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') {
      this.fail('failed'); return;
    }
    if(this.value.status === 'requesting_qr') this.deadline = this.clock() + this.lifetimeMs;
    this.value = {status: 'qr_ready', png_base64: png.toString('base64')};
  }
  succeeded(event) {
    if (!this.active() || this.status().status === 'expired') return;
    const uin = String(event?.uin ?? '');
    if (!/^[1-9][0-9]{4,19}$/.test(uin)) {this.fail('failed'); return;}
    this.value = {status: 'authenticated', uin};
  }
  fail(status = 'failed') {
    if (!this.closed && !['authenticated', 'expired', 'failed', 'picture_timeout', 'verification_required'].includes(this.value.status)) this.value = {status};
  }
  close() {this.closed = true; this.value = {status: 'cancelled'};}
}
module.exports = {LoginState};
