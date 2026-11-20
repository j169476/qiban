'use strict';
// SPDX-License-Identifier: Apache-2.0
// Engine/login initialization adapted from Waylay 3dd3572a4e34f5b2f91ad6d645a16fe6cc34eadb.
// Qiban: isolated Windows data; no quick-login, chat session, send or implicit QR refresh.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {createListener} = require('./listener.cjs');
const {LoginState} = require('./login_state.cjs');
const {loginAccounts} = require('./login_accounts.cjs');
class LoginSession {
  constructor(config) {this.config = config;}
  async open() {
    if (process.platform !== 'win32' || process.version !== 'v24.15.0') throw Error('qq_native_runtime_version');
    const config = this.config, host = {exports: {}};
    process.dlopen(host, path.join(config.runtime, 'QQNT.dll'));
    this.lifecycle = host.exports;
    process.on('exit', () => this.lifecycle.beginShutdown());
    const wrapper = require(path.join(config.runtime, 'wrapper.node'));
    const data = path.join(config.data, 'login'); fs.mkdirSync(data, {recursive: true});
    wrapper.NodeIQQNTWrapperEngine.get().initWithDeskTopConfig({base_path_prefix: '', platform_type: 3, app_type: 4,
      app_version: '9.9.28-46928', os_version: os.version(), use_xlog: true,
      qua: 'V1_WIN_NQ_9.9.28_46928_GW_B', global_path_config: {desktopGlobalPath: data},
      thumb_config: {maxSide: 324, minSide: 48, longLimit: 6, density: 2}}, createListener(null));
    const login = wrapper.NodeIKernelLoginService.get();
    const state = this.state = new LoginState(login);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {state.fail(); reject(Error('qq_login_timeout'));}, 45000);
      login.initConfig({machineId: '', appid: '537345957', platVer: os.version(), commonPath: data,
        clientVer: '9.9.28-46928', hostName: os.hostname(), externalVersion: false});
      login.addKernelLoginListener(createListener('login', {
        onLoginConnected: () => {state.connected(); clearTimeout(timer); resolve();},
        onQRCodeGetPicture: event => state.picture(event),
        onQRCodeSessionUserScaned: () => state.scanned(),
        onQRCodeSessionFailed: () => {
          const phase=state.status().status;
          if (phase === 'requesting_qr') state.fail();
          else if (phase === 'qr_ready' || phase === 'qr_scanned') state.fail('expired');
        },
        onQRCodeLoginSucceed: event => state.succeeded(event),
        onLoginFailed: () => {state.fail(); clearTimeout(timer); reject(Error('qq_login_failed'));}
      }));
      login.connect();
    });
    return state.status();
  }
  async accounts() {return {accounts: loginAccounts(await this.state.login.getLoginList())};}
  close() {this.state?.close(); this.lifecycle?.beginShutdown();}
}
module.exports = {LoginSession};
