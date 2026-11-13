'use strict';
// SPDX-License-Identifier: MIT
// Only explicit user actions request a QR; callbacks cannot restart a login.
class LoginState {
  constructor(login, {clock = () => Date.now(), lifetimeMs = 300000} = {}) {
    this.login = login; this.clock = clock; this.lifetimeMs = lifetimeMs;
    this.value = {status: 'connecting'}; this.deadline = null; this.closed = false;
  }
  connected() {if (!this.closed && this.value.status === 'connecting') this.value = {status: 'ready'};}
  active() {return !this.closed && ['requesting_qr', 'qr_ready'].includes(this.value.status);}
  status() {
    if (this.active() && this.clock() >= this.deadline) this.fail('expired');
    return {...this.value};
  }
  request() {
    if (this.closed || this.status().status !== 'ready') throw Error('qq_login_state');
    this.value = {status: 'requesting_qr'}; this.deadline = this.clock() + this.lifetimeMs;
    try {
      Promise.resolve(this.login.getQRCodePicture()).catch(() => this.fail('failed'));
    } catch {this.fail('failed');}
    return this.status();
  }
  picture(event) {
    if (!this.active() || this.status().status === 'expired') return;
    const encoded = event?.pngBase64QrcodeData;
    if (typeof encoded !== 'string' || encoded.length > 350000) {this.fail('failed'); return;}
    const png = Buffer.from(encoded.replace(/^data:image\/png;base64,/, ''), 'base64');
    if (png.length > 262144 || png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') {
      this.fail('failed'); return;
    }
    this.value = {status: 'qr_ready', png_base64: png.toString('base64')};
  }
  succeeded(event) {
    if (!this.active() || this.status().status === 'expired') return;
    const uin = String(event?.uin ?? '');
    if (!/^[1-9][0-9]{4,19}$/.test(uin)) {this.fail('failed'); return;}
    this.value = {status: 'authenticated', uin};
  }
  fail(status = 'failed') {
    if (!this.closed && !['authenticated', 'expired', 'failed'].includes(this.value.status)) this.value = {status};
  }
  close() {this.closed = true; this.value = {status: 'cancelled'};}
}
module.exports = {LoginState};
