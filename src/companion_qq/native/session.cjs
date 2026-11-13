'use strict';
// SPDX-License-Identifier: Apache-2.0
// Initialization adapted from Waylay src/bridge.js at
// 3dd3572a4e34f5b2f91ad6d645a16fe6cc34eadb; see LICENSE.Waylay.
// Qiban: Windows-only pinned runtime, verified catalog, bounded reads and text sends.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {createListener} = require('./listener.cjs');
const {MessageSyncBarrier} = require('./sync_barrier.cjs');
const {recoverGroupWindow, queryGroupHead} = require('./group_recovery.cjs');
const {recoverPrivateWindow} = require('./private_recovery.cjs');
const {capturePrivateHead} = require('./private_head.cjs');
const {updateGroups, currentFriends} = require('./catalog.cjs');
const {TextSender} = require('./text_sender.cjs');
const {MediaDownloader} = require('./media_download.cjs');
class NativeSession {
  constructor(config) {
    this.config = config; this.gate = new MessageSyncBarrier(); this.closed = false;
    this.catalog = {uin: config.account.uin, uid: config.account.uid, friends: {}, groups: []};
    this.rosterSerial = 0;
    if (config.follow) this.catalog.tenures = {private:{}, group:{}};
    this.groupUpdates = [];
    this.activity = []; this.historyQueries = [];
  }
  guard() { if (this.closed || !this.ready) throw Error('qq_native_not_ready'); }
  async open() {
    if (process.platform !== 'win32' || process.version !== 'v24.15.0') throw Error('qq_native_runtime_version');
    const config = this.config, account = config.account;
    if (typeof config.media_session !== 'string' || !/^[a-f0-9]{64}$/.test(config.media_session)) throw Error('qq_media_path');
    const host = {exports: {}};
    process.dlopen(host, path.join(config.runtime, 'QQNT.dll'));
    this.lifecycle = host.exports;
    process.on('exit', () => this.lifecycle.beginShutdown());
    const wrapper = require(path.join(config.runtime, 'wrapper.node'));
    const data = path.join(config.data, 'login'); fs.mkdirSync(data, {recursive: true});
    wrapper.NodeIQQNTWrapperEngine.get().initWithDeskTopConfig({base_path_prefix: '', platform_type: 3, app_type: 4,
      app_version: '9.9.28-46928', os_version: os.version(), use_xlog: true,
      qua: 'V1_WIN_NQ_9.9.28_46928_GW_B', global_path_config: {desktopGlobalPath: data},
      thumb_config: {maxSide: 324, minSide: 48, longLimit: 6, density: 2}}, createListener(null));
    const login = wrapper.NodeIKernelLoginService.get(); this.login = login;
    await new Promise((resolve, reject) => {
      let requested = false;
      const timer = setTimeout(() => reject(Error('qq_login_timeout')), 45000);
      const fail = code => {clearTimeout(timer); this.closed = true; reject(Error(code));};
      const init = () => {
        const raw = login.getMachineGuid();
        if (typeof raw !== 'string' || !/^[a-fA-F0-9]{32}$/.test(raw)) throw Error('qq_native_machine_identity');
        const guid = [raw.slice(0, 8), raw.slice(8, 12), raw.slice(12, 16), raw.slice(16, 20), raw.slice(20)].join('-');
        const accounts = path.join(config.data, 'accounts'), downloads = path.join(config.data, 'downloads');
        fs.mkdirSync(accounts, {recursive: true}); fs.mkdirSync(downloads, {recursive: true});
        this.startup = wrapper.NodeIQQNTStartupSessionWrapper.create();
        const session = this.session = wrapper.NodeIQQNTWrapperSession.getNTWrapperSession('nt_1');
        const options = {selfUin: account.uin, selfUid: account.uid, desktopPathConfig: {account_path: accounts},
          clientVer: '9.9.28-46928', a2: '', d2: '', d2Key: '', machineId: '', platform: 3, platVer: os.release(), appid: '537345957',
          rdeliveryConfig: {appKey: '', systemId: 0, appId: '', logicEnvironment: '', platform: 3, language: '', sdkVersion: '',
            userId: '', appVersion: '', osVersion: '', bundleId: '', serverUrl: '', fixedAfterHitKeys: ['']},
          defaultFileDownloadPath: downloads, deviceInfo: {guid, buildVer: '9.9.28-46928', localId: 2052,
            devName: os.hostname(), devType: os.type(), vendorName: '', osVer: os.release(), vendorOsName: os.type(),
            setMute: false, vendorType: 0}, deviceConfig: '{"appearance":{"isSplitViewMode":true},"msg":{}}'};
        session.init(options, {onMSFStatusChange() {}, onMSFSsoError: () => {this.closed = true;}, getGroupCode() {}},
          {dispatchRequest() {}, dispatchCall() {}, dispatchCallWithJson() {}},
          createListener('session', {onSessionInitComplete: () => {
            try {
              session.getMsgService().addKernelMsgListener(createListener('messages', {}, event => {
                this.gate.observe(event.eventName);
                this.media?.observe(event.eventName, event.data);
                const rows = Array.isArray(event.data) ? event.data.flat(1) : event.data?.msgList || [event.data];
                this.sender?.observe(event.eventName, rows);
                this.activity.push({name: event.eventName, time: Date.now(),
                  group_rows: rows.filter(row => row?.chatType === 2 && config.groups.includes(row.peerUid)).length});
                if (this.activity.length > 100) this.activity.shift();
              }));
              this.ready = true; clearTimeout(timer); resolve();
            } catch { fail('qq_native_session_failed'); }
          }}));
        this.startup.start();
      };
      login.initConfig({machineId: '', appid: '537345957', platVer: os.version(), commonPath: data,
        clientVer: '9.9.28-46928', hostName: os.hostname(), externalVersion: false});
      login.addKernelLoginListener(createListener('login', {
        onLoginConnected: () => {
          if (requested) return; requested = true;
          (async () => {
            const list = await login.getLoginList();
            if (list.result !== 0 || !Array.isArray(list.LocalLoginInfoList)) throw Error('qq_login_required');
            const selected = list.LocalLoginInfoList.filter(item => String(item.uin) === account.uin);
            if (selected.length !== 1 || selected[0].uid !== account.uid) throw Error('qq_login_required');
            const result = await login.quickLoginWithUin(account.uin);
            if (Number(result.result) !== 0) throw Error('qq_login_required');
            init();
          })().catch(error => fail(error.message === 'qq_login_required' ? error.message : 'qq_native_session_failed'));
        }, onLoginFailed: () => fail('qq_login_required')
      }));
      login.connect();
    });
    await this.verifyCatalog();
    await this.gate.wait({timeoutMs: 45000}); this.guard();
    this.media = new MediaDownloader({service: this.session.getMsgService(),
      root: path.join(config.data, 'media-staging', config.media_session),
      cacheRoots: {
        audio: path.join(config.data, 'accounts', account.uin, 'nt_qq', 'nt_data', 'Ptt'),
        image: path.join(config.data, 'accounts', account.uin, 'nt_qq', 'nt_data', 'Pic')},
      guard: () => {this.guard(); this.gate.assertCurrent(this.gate.generation); return this.gate.generation;}});
    this.sender = new TextSender({service: this.session.getMsgService(), mediaRoot: this.media.root, imageCacheRoot: this.media.cacheRoots.image,
      fileCacheRoot: path.join(config.data, 'accounts', account.uin, 'nt_qq', 'nt_data', 'File'),
      serverTime: () => this.session.getMSFService().getServerTime(), account: {uin: account.uin, uid: account.uid},
      guard: () => {this.guard(); this.gate.assertCurrent(this.gate.generation); return this.gate.generation;},
      membership: (kind,target) => this.catalog.tenures?.[kind]?.[target] ?? null,
      resolvePeer: (kind, target) => {
        if (kind === 'group' && this.catalog.groups.includes(target)) return {chatType: 2, peerUid: target, guildId: ''};
        if (kind === 'private' && this.catalog.friends[target]) return {chatType: 1, peerUid: this.catalog.friends[target], guildId: ''};
        throw Error('qq_target_unverified');
      }});
    return this.catalog;
  }
  updateTenures(kind, oldIds, newIds) {
    if (!this.config.follow?.[kind]) return;
    const previous = this.catalog.tenures[kind], next = {};
    for (const [id,uid] of Object.entries(newIds)) {
      next[id] = previous[id] && oldIds[id] === uid ? previous[id] : ++this.rosterSerial;
    }
    this.catalog.tenures[kind] = next;
  }
  async verifyCatalog() {
    const {private_targets: peers, groups} = this.config;
    const follow = this.config.follow || {};
    const checks = [];
    if (peers.length || follow.private) checks.push(new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('qq_target_unverified')), 45000);
      this.session.getBuddyService().addKernelBuddyListener(createListener('buddy', {onBuddyListChange: categories => {
        try {
          const friends=currentFriends(categories, follow.private ? null : peers);
          this.updateTenures('private',this.catalog.friends,friends);
          this.catalog.friends=friends;
        } catch (error) {
          this.updateTenures('private',this.catalog.friends,{});
          this.catalog.friends = {};
          clearTimeout(timer);
          reject(error);
          return;
        }
        if (follow.private || peers.every(peer => this.catalog.friends[peer])) {clearTimeout(timer); resolve();}
      }}));
      Promise.resolve(this.session.getBuddyService().getBuddyList(true)).catch(() => {clearTimeout(timer); reject(Error('qq_native_catalog_invalid'));});
    }));
    if (groups.length || follow.group) checks.push(new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('qq_target_unverified')), 45000);
      this.session.getGroupService().addKernelGroupListener(createListener('groups', {}, event => {
        if (event.eventName !== 'onGroupListUpdate') return;
        const entries = Array.isArray(event.data) ? event.data[1] : event.data?.groupList;
        if (!Array.isArray(entries)) {clearTimeout(timer); reject(Error('qq_native_catalog_invalid')); return;}
        const mode = Array.isArray(event.data) ? event.data[0] : null;
        this.groupUpdates.push({mode: ['number', 'boolean', 'string'].includes(typeof mode) ? String(mode).slice(0, 40) : typeof mode,
          count: entries.length, configured_matches: groups.filter(group => entries.some(entry => String(entry.groupCode) === group)).length});
        if (this.groupUpdates.length > 16) this.groupUpdates.shift();
        try {
          const next = updateGroups(this.catalog.groups, mode, entries, follow.group ? null : groups);
          if (follow.group) {
            const asMap = ids => Object.fromEntries(ids.map(id=>[id,id]));
            this.updateTenures('group',asMap(this.catalog.groups),asMap(next));
          }
          this.catalog.groups=next;
        }
        catch {clearTimeout(timer); this.closed = true; reject(Error('qq_native_catalog_invalid')); return;}
        if (follow.group ? mode === 0 || mode === 1 : this.catalog.groups.length === groups.length) {clearTimeout(timer); resolve();}
      }));
      Promise.resolve(this.session.getGroupService().getGroupList(true)).catch(() => {clearTimeout(timer); reject(Error('qq_native_catalog_invalid'));});
    }));
    await Promise.all(checks); this.guard();
  }
  async queryContacts(peers) {
    const generation = await this.gate.wait();
    const check = () => {this.guard(); this.gate.assertCurrent(generation);};
    check();
    const recent = this.session.getRecentContactService();
    // A fresh account cache can finish message sync before its recent-contact
    // index has been requested. Initialize it explicitly once per sync cycle.
    if (this.contactsGeneration !== generation) {
      const initialized = await recent.getRecentContactList(); check();
      if (initialized?.result !== 0) throw Error('Group contact initialization failed');
      this.contactsGeneration = generation;
    }
    const result = await recent.getContacts(peers); check();
    return result;
  }
  async groupHead(group, allowEmpty = false) {
    const generation = await this.gate.wait();
    const check = () => {this.guard(); this.gate.assertCurrent(generation);
      if (!this.catalog.groups.includes(group)) throw Error('qq_target_unverified');};
    return queryGroupHead({account: this.catalog.uin, group, assertCurrent: check, allowEmpty,
      contactQuery: peers => this.queryContacts(peers), service: this.session.getMsgService()});
  }
  async groupWindow(group, lower, upper = null, pageSize = 100, since = null) {
    this.guard(); if (!this.catalog.groups.includes(group)) throw Error('qq_target_unverified');
    if (!Number.isInteger(pageSize) || pageSize < 2 || pageSize > 100) throw Error('qq_native_protocol_invalid');
    const native = this.session.getMsgService();
    const service = {
      getLatestDbMsgs: (...args) => native.getLatestDbMsgs(...args),
      getSingleMsg: (...args) => native.getSingleMsg(...args),
      getMsgsIncludeSelf: async (...args) => {
        const before = Date.now(); const result = await native.getMsgsIncludeSelf(...args);
        this.historyQueries.push({before, after: Date.now(), count: args[2], result: result?.result,
          result_type: typeof result?.result, messages: result?.msgList?.length});
        if (this.historyQueries.length > 20) this.historyQueries.shift();
        return result;
      }
    };
    return recoverGroupWindow({account: this.catalog.uin, group, lowerCheckpoint: lower, upperCheckpoint: upper,
      contactQuery: peers => this.queryContacts(peers), gate: this.gate, service,
      guard: () => {this.guard(); if (!this.catalog.groups.includes(group)) throw Error('qq_target_unverified');},
      pageSize, since, maxPages: 100});
  }
  privateContext(target) {
    this.guard();
    const uid = this.catalog.friends[target];
    if (!uid) throw Error('qq_target_unverified');
    return {account: {uin: this.catalog.uin, uid: this.catalog.uid}, peer: {uin: target, uid},
      gate: this.gate, service: this.session.getMsgService(),
      guard: () => {this.guard(); if (this.catalog.friends[target] !== uid) throw Error('qq_target_unverified');}};
  }
  async privateHead(target, allowEmpty = false) {return capturePrivateHead({...this.privateContext(target), allowEmpty});}
  async privateWindow(target, lower, upper, pageSize = 100, since = null) {
    if (!Number.isInteger(pageSize) || pageSize < 2 || pageSize > 100) throw Error('qq_native_protocol_invalid');
    return recoverPrivateWindow({...this.privateContext(target), lowerCheckpoint: lower,
      upperCheckpoint: upper, pageSize, since, maxPages: 100});
  }
  close() {this.sender?.close(); this.media?.close(); this.closed = true; this.ready = false; this.gate.invalidate(); this.startup?.stop();}
}
module.exports = {NativeSession};
