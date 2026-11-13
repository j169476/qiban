'use strict';
// SPDX-License-Identifier: MIT
// Transport primitive: only a verified history window may supply a message.
// This does not grant model access or implement the managed asset lifecycle.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const requireValue = (ok, code = 'qq_media_invalid') => {if (!ok) throw Error(code);};
const MAX_BYTES = 10 * 1024 * 1024;
class MediaDownloader {
  constructor({service, root, cacheRoots = {}, guard, timeoutMs = 60000}) {
    this.service = service; this.root = path.resolve(root); this.guard = guard;
    this.cacheRoots = Object.fromEntries(Object.entries(cacheRoots).map(([kind, folder]) => [kind, path.resolve(folder)]));
    this.diagnostic = null;
    this.timeoutMs = timeoutMs; this.pending = null; this.closed = false; this.failed = false;
  }
  observe(name, info) {
    if (name !== 'onRichMediaDownloadComplete' || !this.pending) return;
    const item = this.pending;
    if (String(info?.msgId) !== item.msgId || String(info?.msgElementId) !== item.elementId) return;
    item.resolve(info);
  }
  async download(message, position, maxBytes = MAX_BYTES) {
    requireValue(!this.closed && !this.failed && !this.pending, 'qq_media_state');
    requireValue(Number.isSafeInteger(maxBytes) && maxBytes > 0 && maxBytes <= MAX_BYTES, 'qq_media_limit');
    this.diagnostic = {phase:'validation'};
    const epoch = this.guard();
    requireValue(message && [1, 2].includes(message.chatType) && typeof message.peerUid === 'string');
    requireValue(Number.isInteger(position) && position >= 0 && position < 200);
    const element = message.elements?.[position];
    const types = {2: ['image', 'picElement'], 3: ['file', 'fileElement'], 4: ['audio', 'pttElement']};
    const type = types[element?.elementType];
    requireValue(type && typeof element[type[1]] === 'object' && element[type[1]] !== null);
    const meta = element[type[1]], msgId = String(message.msgId), elementId = String(element.elementId);
    requireValue(/^[0-9]{1,20}$/.test(msgId) && /^[0-9]{1,20}$/.test(elementId));
    requireValue(typeof meta.fileSize === 'number' ||
      (typeof meta.fileSize === 'string' && /^[0-9]{1,20}$/.test(meta.fileSize)));
    const declared = Number(meta.fileSize);
    requireValue(Number.isSafeInteger(declared) && declared >= 0);
    requireValue(declared <= maxBytes, 'qq_media_limit');
    // Private file metadata may leave fileMd5 empty. Never skip verification:
    // the alternate digest must equal the MD5 of the entire downloaded file,
    // not merely a prefix. Larger files remain outside our bounded allowance.
    let expected = type[0] === 'file' ? meta.fileMd5 : meta.md5HexStr;
    if (type[0] === 'file' && message.chatType === 1 && (expected == null || expected === ''))
      expected = meta.file10MMd5;
    requireValue(typeof expected === 'string' && /^[a-fA-F0-9]{32}$/.test(expected));
    this.diagnostic.phase='staging';
    fs.mkdirSync(this.root, {recursive: true});
    requireValue(!fs.lstatSync(this.root).isSymbolicLink(), 'qq_media_path');
    const root = fs.realpathSync(this.root);
    const token = crypto.randomBytes(16).toString('hex');
    const destination = path.join(root, token + '.part');
    // A unique, exclusively reserved destination; no peer-controlled file name.
    fs.closeSync(fs.openSync(destination, 'wx'));
    let timer;
    const receipt = new Promise((resolve, reject) => {
      this.pending = {msgId, elementId, resolve, reject};
      timer = setTimeout(() => reject(Error('qq_media_timeout')), this.timeoutMs);
    });
    try {
      const [, done] = await Promise.all([
        Promise.resolve().then(async () => {
          requireValue(!this.closed && this.guard() === epoch, 'qq_media_state');
          this.diagnostic.phase='native_request';
          const result = await this.service.downloadRichMedia({fileModelId: '0', downSourceType: 0, triggerType: 1,
            msgId, chatType: message.chatType, peerUid: message.peerUid, elementId,
            thumbSize: 0, downloadType: 1, filePath: destination});
          // Fail immediately on a rejected request, even without a callback.
          // Some successful SDK methods return void; still require completion.
          requireValue(result == null || result === 0 || result.result === 0, 'qq_media_download_failed');
        }), receipt
      ]);
      this.diagnostic.phase='completion';
      this.diagnostic.file_error=String(done.fileErrCode).slice(0,20);
      this.diagnostic.server_error=String(done.fileSrvErrCode).slice(0,20);
      requireValue(!this.closed && this.guard() === epoch, 'qq_media_state');
      requireValue(String(done.fileErrCode) === '0' && String(done.fileSrvErrCode) === '0', 'qq_media_download_failed');
      requireValue(typeof done.filePath === 'string' && path.isAbsolute(done.filePath), 'qq_media_path');
      this.diagnostic.phase='source_path';
      const source = path.resolve(done.filePath);
      if (source !== destination) {
        // The pinned SDK returns Ptt/Pic cache paths for voice/images even with
        // filePath supplied. Only the matching own-account cache is accepted.
        const cache = this.cacheRoots[type[0]];
        requireValue(['audio', 'image'].includes(type[0]) && cache, 'qq_media_path');
        const relative = path.relative(cache, source);
        requireValue(relative && !relative.startsWith('..' + path.sep) && relative !== '..' &&
          !path.isAbsolute(relative) && fs.realpathSync(cache) === cache, 'qq_media_path');
      }
      this.diagnostic.phase='source_read';
      const stat = fs.lstatSync(source);
      requireValue(stat.isFile() && !stat.isSymbolicLink() && fs.realpathSync(source) === source, 'qq_media_path');
      requireValue((stat.size > 0 || type[0] === 'file') && stat.size <= maxBytes, 'qq_media_limit');
      // Bounded read even if the SDK changes the file after the stat.
      const fd = fs.openSync(source, 'r'), bytes = Buffer.alloc(maxBytes + 1);
      let length = 0;
      try {
        while (length < bytes.length) {
          const count = fs.readSync(fd, bytes, length, bytes.length - length, null);
          if (!count) break;
          length += count;
        }
      } finally {fs.closeSync(fd);}
      requireValue((length > 0 || type[0] === 'file') && length <= maxBytes, 'qq_media_limit');
      const content = bytes.subarray(0, length);
      this.diagnostic.phase='integrity';
      const md5 = crypto.createHash('md5').update(content).digest('hex');
      requireValue(md5 === expected.toLowerCase(), 'qq_media_integrity');
      // QQ image/voice metadata can describe a different cached representation.
      // For a generic file the declared length must describe the original bytes.
      requireValue(type[0] !== 'file' || length === declared, 'qq_media_integrity');
      requireValue(!this.closed && this.guard() === epoch, 'qq_media_state');
      this.diagnostic.phase='publish';
      const verified = path.join(root, token + '.bin');
      // Publish the verified bytes, rather than a path still owned by the SDK.
      fs.writeFileSync(verified, content, {flag: 'wx'});
      fs.unlinkSync(destination);
      this.diagnostic.phase='verified';
      return {kind: type[0], path: verified, bytes: length, md5,
        sha256: crypto.createHash('sha256').update(content).digest('hex'), declared_bytes_match: length === declared};
    } catch (error) {
      // A timed-out SDK request may still write. Require process shutdown before
      // reuse/cleanup; never expose its partial file as a managed asset.
      this.diagnostic.error_name=['Error','TypeError','RangeError'].includes(error.name)?error.name:'other';
      this.diagnostic.error_code=typeof error.code==='string' && /^[A-Z_0-9]{1,32}$/.test(error.code)?error.code:null;
      this.failed = true;
      throw error;
    } finally {clearTimeout(timer); this.pending = null;}
  }
  close() {this.closed = true; this.pending?.reject(Error('qq_media_state'));}
}
module.exports = {MediaDownloader, MAX_BYTES};
