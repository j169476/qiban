'use strict';
// SPDX-License-Identifier: MIT
const crypto = require('node:crypto'), fs = require('node:fs'), path = require('node:path');
const requireValue = (condition, code = 'qq_native_protocol_invalid') => {if (!condition) throw Error(code);};
class TextSender {
  constructor({service, serverTime, account, resolvePeer, membership = () => null, guard, mediaRoot, imageCacheRoot, fileCacheRoot, timeoutMs = 15000}) {
    Object.assign(this, {service, serverTime, account, resolvePeer, guard, mediaRoot, imageCacheRoot, fileCacheRoot, timeoutMs});
    this.membership = membership;
    this.pending = this.active = null; this.diagnostic = null; this.failed = this.closed = false;
  }
  prepare(kind, target, text) {
    const epoch = this.guard();
    requireValue(!this.closed && !this.failed && !this.pending && !this.active, 'qq_native_send_state');
    requireValue(['group', 'private'].includes(kind) && typeof text === 'string' && text.trim() && Array.from(text).length <= 1000);
    const peer = this.resolvePeer(kind, target);
    const time = this.serverTime();
    requireValue(typeof time === 'string' && /^[0-9]+$/.test(time));
    const id = this.service.generateMsgUniqueId(peer.chatType, time);
    requireValue(typeof id === 'string' && /^[1-9][0-9]{0,19}$/.test(id));
    const token = crypto.randomBytes(32).toString('hex');
    this.pending = {token, id, peer, kind, target, text, epoch, tenure:this.membership(kind,target)};
    return {token, message_id: id};
  }
  prepareVoice(kind, target, encoded, duration) {
    requireValue(typeof encoded === 'string' && encoded.length <= 1400000 &&
      /^[A-Za-z0-9+/]+={0,2}$/.test(encoded) && Number.isInteger(duration) && duration > 0 && duration <= 600000);
    const audio = Buffer.from(encoded, 'base64');
    requireValue(audio.length > 10 && audio.length <= 1024 * 1024 && audio.subarray(0, 10).equals(Buffer.from('\x02#!SILK_V3')));
    requireValue(typeof this.mediaRoot === 'string');
    const ticket = this.prepare(kind, target, 'voice');
    const file = path.join(this.mediaRoot, crypto.randomBytes(16).toString('hex') + '.bin');
    try {
      fs.writeFileSync(file, audio, {flag: 'wx'});
      this.pending.voice = {file, md5: crypto.createHash('md5').update(audio).digest('hex'),
        bytes: audio.length, duration: Math.ceil(duration / 1000)};
      return ticket;
    } catch (error) { this.pending = null; throw error; }
  }
  prepareImage(kind, target, encoded, width, height, format) {
    const extensions = {PNG: 'png', JPEG: 'jpg', GIF: 'gif', WEBP: 'webp'};
    requireValue(typeof encoded === 'string' && encoded.length <= 13981016 && /^[A-Za-z0-9+/]+={0,2}$/.test(encoded));
    requireValue(Object.hasOwn(extensions, format) && Number.isInteger(width) && Number.isInteger(height) &&
      width > 0 && height > 0 && width * height <= 16000000);
    const bytes = Buffer.from(encoded, 'base64');
    requireValue(bytes.length > 0 && bytes.length <= 10 * 1024 * 1024 && bytes.toString('base64') === encoded);
    const signatures = {PNG: bytes.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex')),
      JPEG: bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255,
      GIF: ['GIF87a','GIF89a'].includes(bytes.subarray(0,6).toString()),
      WEBP: bytes.subarray(0,4).toString() === 'RIFF' && bytes.subarray(8,12).toString() === 'WEBP'};
    requireValue(signatures[format] && typeof this.mediaRoot === 'string');
    const ticket = this.prepare(kind, target, 'sticker');
    const name = crypto.randomBytes(16).toString('hex') + '.' + extensions[format];
    const file = path.join(this.mediaRoot, name);
    try {
      fs.writeFileSync(file, bytes, {flag: 'wx'});
      const source = require('./image_source.cjs').registerImage(this.service, this.imageCacheRoot, bytes, name);
      this.pending.image = {file, source, name, md5: crypto.createHash('md5').update(bytes).digest('hex'),
        bytes: bytes.length, width, height};
      return ticket;
    } catch (error) {fs.rmSync(file, {force: true}); this.pending = null; throw error;}
  }
  prepareFile(kind, target, encoded, name) {
    requireValue(typeof encoded === 'string' && encoded.length <= 13981016 && /^[A-Za-z0-9+/]+={0,2}$/.test(encoded));
    requireValue(typeof name === 'string' && Array.from(name).length > 0 && Array.from(name).length <= 180 &&
      !/[<>:"/\\|?*\x00-\x1f\x7f]/.test(name) && !/[. ]$/.test(name) &&
      !/^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?$/i.test(name));
    const bytes = Buffer.from(encoded, 'base64');
    requireValue(bytes.length > 0 && bytes.length <= 10 * 1024 * 1024 && bytes.toString('base64') === encoded);
    requireValue(typeof this.mediaRoot === 'string');
    const ticket = this.prepare(kind, target, 'file');
    const file = path.join(this.mediaRoot, crypto.randomBytes(16).toString('hex') + '.bin');
    try {
      fs.writeFileSync(file, bytes, {flag: 'wx'});
      const source = require('./image_source.cjs').registerFile(this.service, this.fileCacheRoot, bytes, name);
      this.pending.file = {file, source, name, md5: crypto.createHash('md5').update(bytes).digest('hex'), bytes: bytes.length};
      return ticket;
    } catch(error) {fs.rmSync(file, {force:true}); this.pending = null; throw error;}
  }
  discard(item) {
    if (item?.file) fs.rmSync(item.file.file, {force:true});
    if (item?.voice) fs.rmSync(item.voice.file, {force: true});
    if (item?.image) fs.rmSync(item.image.file, {force: true});
  }
  cancel(token) {
    requireValue(this.pending?.token === token, 'qq_native_send_state');
    this.discard(this.pending); this.pending = null;
    return {cancelled: true};
  }
  observe(name, rows) {
    const active = this.active;
    if (!active || !['onAddSendMsg', 'onMsgInfoListUpdate', 'onRecvMsg', 'onRecvActiveMsg'].includes(name)) return;
    for (const row of rows) {
      if (row?.msgId !== active.id || row.chatType !== active.peer.chatType || row.peerUid !== active.peer.peerUid ||
          String(row.senderUin) !== this.account.uin || row.senderUid !== this.account.uid) continue;
      if (Number.isSafeInteger(row.sendStatus)) this.diagnostic.event_status = row.sendStatus;
      if (row.sendStatus !== 2) continue;
      try {
        requireValue(this.guard() === active.epoch);
        requireValue(this.resolvePeer(active.kind, active.target).peerUid === active.peer.peerUid);
        requireValue(typeof row.msgSeq === 'string' && /^[1-9][0-9]{0,19}$/.test(row.msgSeq));
        requireValue(Array.isArray(row.elements) && row.elements.length === 1);
        if (active.file) {
          const file = row.elements[0].fileElement;
          requireValue(row.elements[0].elementType === 3 && file?.fileMd5?.toLowerCase() === active.file.md5 &&
            file.fileName === active.file.name && String(file.fileSize) === String(active.file.bytes));
        } else if (active.voice) {
          requireValue(row.elements[0].elementType === 4 &&
            row.elements[0].pttElement?.md5HexStr?.toLowerCase() === active.voice.md5);
        } else if (active.image) {
          requireValue(row.elements[0].elementType === 2 &&
            row.elements[0].picElement?.md5HexStr?.toLowerCase() === active.image.md5);
        } else requireValue(row.elements[0].elementType === 1 &&
          row.elements[0].textElement?.content === active.text && row.elements[0].textElement?.atType === 0);
        active.confirmed = true;
        active.resolve({message_id: active.id, reference: active.kind === 'group' ? 'native-seq:' + row.msgSeq : null});
      } catch {this.diagnostic.phase = 'receipt_mismatch'; active.reject(Error('qq_native_send_uncertain'));}
    }
  }
  async submit(token) {
    requireValue(this.pending?.token === token && !this.active && !this.closed && !this.failed, 'qq_native_send_state');
    const item = this.pending;
    requireValue(this.guard() === item.epoch, 'qq_native_send_state');
    requireValue(this.resolvePeer(item.kind, item.target).peerUid === item.peer.peerUid, 'qq_target_unverified');
    requireValue(this.membership(item.kind,item.target) === item.tenure, 'qq_target_unverified');
    this.pending = null; // Consume before the first possible native invocation.
    const diagnostic = this.diagnostic = {phase: 'waiting', native_result: null, event_status: null};
    let timer;
    const receipt = new Promise((resolve, reject) => {
      this.active = {...item, resolve, reject};
    });
    // The deadline covers both native return and receipt. A resolved receipt
    // must not disable the deadline while sendMsg's promise remains pending.
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {diagnostic.phase = this.active?.confirmed ? 'native_return_timeout' : 'receipt_timeout';
        reject(Error('qq_native_send_uncertain'));}, this.timeoutMs);
    });
    try {
      const nativeReturn = Promise.resolve().then(() => {
          requireValue(!this.closed, 'qq_native_send_state');
          requireValue(this.guard() === item.epoch, 'qq_native_send_state');
          const elements = item.file ? [{elementType:3,elementId:'',fileElement:{
            fileName:item.file.name,filePath:item.file.source,fileSize:String(item.file.bytes),
            folderId:'',fileMd5:item.file.md5,picThumbPath:new Map()}}] : item.image ? [{elementType: 2, elementId: '', picElement: {
            md5HexStr: item.image.md5, fileSize: String(item.image.bytes), fileName: item.image.name,
            sourcePath: item.image.source, original: true, picType: 1000, picSubType: 0,
            picWidth: item.image.width, picHeight: item.image.height, fileUuid: '', fileSubId: '',
            thumbFileSize: 0, summary: '[图片]'}}] : item.voice ? [{elementType: 4, elementId: '', pttElement: {
            fileName: 'voice.silk', filePath: item.voice.file, md5HexStr: item.voice.md5,
            fileSize: String(item.voice.bytes), duration: item.voice.duration, formatType: 1, voiceType: 1,
            voiceChangeType: 0, canConvert2Text: true, waveAmplitudes: [], fileSubId: '', playState: 1,
            autoConvertText: 0, otherBusinessInfo: {aiVoiceType: 0}}}] :
            [{elementType: 1, elementId: '', textElement: {content: item.text, atType: 0, atUid: '', atTinyId: '', atNtUid: ''}}];
          diagnostic.phase = 'invoking';
          return this.service.sendMsg(item.id, item.peer, elements, new Map());
        }).then(result => {
          diagnostic.native_result = Number.isSafeInteger(result?.result) ? result.result : null;
          requireValue(result?.result === 0 || ((item.image || item.file) && result?.result === -1), 'qq_native_send_uncertain');
          return result;
        }).catch(error => {if (diagnostic.phase !== 'confirmed') diagnostic.phase = 'native_rejected'; throw error;});
      // Group file sendMsg may remain pending after the validated terminal
      // message event. That event proves delivery; acceptance alone never does.
      // Keep rejection handlers attached and scoped to this operation so a late
      // native completion cannot corrupt the next send's diagnostic or receipt.
      let confirmed;
      if (item.file) {
        confirmed = await Promise.race([nativeReturn.then(() => new Promise(() => {})), receipt, deadline]);
      } else {
        [, confirmed] = await Promise.race([Promise.all([nativeReturn, receipt]), deadline]);
      }
      requireValue(this.guard() === item.epoch, 'qq_native_send_uncertain');
      diagnostic.phase = 'confirmed';
      this.discard(item);
      return confirmed;
    } catch {
      this.failed = true;
      throw Error('qq_native_send_uncertain');
    } finally {clearTimeout(timer); this.active = null;}
  }
  close() {
    this.closed = true; this.discard(this.pending); this.pending = null;
    this.active?.reject(Error('qq_native_send_uncertain'));
  }
}
module.exports = {TextSender};
