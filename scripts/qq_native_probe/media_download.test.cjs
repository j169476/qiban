'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), crypto = require('node:crypto');
const {MediaDownloader, MAX_BYTES} = require('../../src/companion_qq/native/media_download.cjs');
const content = Buffer.from('文件中文🙂');
const md5 = crypto.createHash('md5').update(content).digest('hex');
function setup(t, action, kind = 'file') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qiban-media-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  let calls = 0, epoch = 1;
  const downloader = new MediaDownloader({root, timeoutMs: 30, guard: () => epoch,
    service: {downloadRichMedia: async request => {calls++; return action(request, downloader);}}});
  const [elementType, field] = {file: [3, 'fileElement'], image: [2, 'picElement'], audio: [4, 'pttElement']}[kind];
  const row = {msgId: '12', chatType: 2, peerUid: '90001', elements: [{elementId: '34', elementType,
    [field]: {fileMd5: md5, md5HexStr: md5, fileSize: content.length}}]};
  return {downloader, row, root, calls: () => calls, change: () => epoch++};
}
function complete(request, downloader, extra = {}) {
  fs.writeFileSync(request.filePath, content);
  downloader.observe('onRichMediaDownloadComplete', {msgId: request.msgId, msgElementId: request.elementId,
    filePath: request.filePath, fileErrCode: 0, fileSrvErrCode: 0, ...extra});
}
test('turn allowance rejects declared overflow before a native download', async t => {
  const h = setup(t, complete);
  await assert.rejects(h.downloader.download(h.row, 0, content.length - 1), /qq_media_limit/);
  assert.equal(h.calls(), 0);
});
test('actual cached representation cannot exceed the remaining turn allowance', async t => {
  const h = setup(t, complete, 'image');
  h.row.elements[0].picElement.fileSize = 1;
  await assert.rejects(h.downloader.download(h.row, 0, content.length - 1), /qq_media_limit/);
  assert.equal(h.calls(), 1);
  assert.ok(fs.readdirSync(h.root).every(name => name.endsWith('.part')));
});
test('exact turn allowance is accepted; invalid allowances never start download', async t => {
  const h = setup(t, complete);
  for (const limit of [0, -1, true, '10', MAX_BYTES + 1])
    await assert.rejects(h.downloader.download(h.row, 0, limit), /qq_media_limit/);
  const result = await h.downloader.download(h.row, 0, content.length);
  assert.equal(result.bytes, content.length); assert.equal(h.calls(), 1);
});
for (const kind of ['file', 'image', 'audio']) test(kind + ' publishes callback-confirmed verified bytes', async t => {
  const h = setup(t, (request, downloader) => {complete(request, downloader); return {result: 0};}, kind);
  const result = await h.downloader.download(h.row, 0);
  assert.equal(result.kind, kind); assert.equal(result.md5, md5);
  assert.deepEqual(fs.readFileSync(result.path), content); assert.equal(h.calls(), 1);
  assert.equal(result.sha256, crypto.createHash('sha256').update(content).digest('hex'));
  assert.deepEqual(fs.readdirSync(h.root), [path.basename(result.path)]);
});
test('no callback is not success even when complete bytes exist; poisoned worker cannot retry', async t => {
  const h = setup(t, request => {fs.writeFileSync(request.filePath, content); return {result: 0};});
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_timeout/);
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_state/); assert.equal(h.calls(), 1);
});
test('unrelated callback cannot complete another attachment', async t => {
  const h = setup(t, (request, downloader) => complete(request, downloader, {msgElementId: '99'}));
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_timeout/);
});
test('download error callback is failure', async t => {
  const h = setup(t, (request, downloader) => complete(request, downloader, {fileSrvErrCode: 1}));
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_download_failed/);
});
test('native rejection fails without waiting for a missing callback', async t => {
  const h = setup(t, () => ({result: 1}));
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_download_failed/);
});
test('callback cannot choose a different local file', async t => {
  const h = setup(t, (request, downloader) => complete(request, downloader, {filePath: __filename}));
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_path/);
});
for (const kind of ['audio', 'image']) test(kind + ' can use its own-account cache but publishes a separate verified copy', async t => {
  let source;
  const h = setup(t, (request, downloader) => {
    source = path.join(h.root, 'Ptt', 'voice.amr'); fs.mkdirSync(path.dirname(source));
    fs.writeFileSync(source, content); complete(request, downloader, {filePath: source});
  }, kind);
  h.downloader.cacheRoots[kind] = path.join(h.root, 'Ptt');
  const result = await h.downloader.download(h.row, 0);
  assert.notEqual(result.path, source); assert.deepEqual(fs.readFileSync(source), content);
  assert.deepEqual(fs.readFileSync(result.path), content);
});
test('voice callback cannot escape its own-account cache', async t => {
  const h = setup(t, (request, downloader) => complete(request, downloader, {filePath: __filename}), 'audio');
  h.downloader.cacheRoots.audio = path.join(h.root, 'Ptt');
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_path/);
});
test('hash mismatch never publishes verified asset', async t => {
  const h = setup(t, complete); h.row.elements[0].fileElement.fileMd5 = '0'.repeat(32);
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_integrity/);
  assert.ok(fs.readdirSync(h.root).every(name => name.endsWith('.part')));
});
test('declared oversize is rejected before native request', async t => {
  const h = setup(t, complete); h.row.elements[0].fileElement.fileSize = MAX_BYTES + 1;
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_limit/); assert.equal(h.calls(), 0);
});
test('empty ordinary file is valid and still requires a successful receipt and matching hash', async t => {
  const h = setup(t, (request, downloader) => {
    complete(request, downloader); fs.truncateSync(request.filePath, 0);
  });
  h.row.elements[0].fileElement.fileSize = 0;
  h.row.elements[0].fileElement.fileMd5 = crypto.createHash('md5').update('').digest('hex');
  const result = await h.downloader.download(h.row, 0); assert.equal(result.bytes, 0);
});
test('native file larger than limit is never read into model material', async t => {
  const h = setup(t, (request, downloader) => {
    complete(request, downloader); fs.truncateSync(request.filePath, MAX_BYTES + 1);
  });
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_limit/);
});
test('file byte count must match metadata independently of hash', async t => {
  const h = setup(t, complete); h.row.elements[0].fileElement.fileSize++;
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_integrity/);
});
test('sync or account generation change rejects completed bytes', async t => {
  const h = setup(t, (request, downloader) => {complete(request, downloader); h.change();});
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_state/);
});
test('closing interrupts wait; late completion cannot publish', async t => {
  let request;
  const h = setup(t, value => {request = value; h.downloader.close();});
  await assert.rejects(h.downloader.download(h.row, 0), /qq_media_state/);
  complete(request, h.downloader); assert.ok(fs.readdirSync(h.root).every(name => name.endsWith('.part')));
});

for (const mismatch of [false,true]) test('private file alternate hash validates full bytes: mismatch='+mismatch,async t=>{
 const h=setup(t,complete);h.row.chatType=1;h.row.peerUid='u_authorized';
 h.row.elements[0].fileElement.fileMd5='';
 h.row.elements[0].fileElement.file10MMd5=mismatch?'0'.repeat(32):md5;
 if(mismatch)await assert.rejects(h.downloader.download(h.row,0),/qq_media_integrity/);
 else {const result=await h.downloader.download(h.row,0);assert.deepEqual(fs.readFileSync(result.path),content);assert.equal(result.md5,md5);}
 assert.equal(h.calls(),1);
});
test('alternate hash cannot hide malformed full hash or absent private hash',async t=>{
 const h=setup(t,complete);h.row.chatType=1;h.row.peerUid='u_authorized';
 const meta=h.row.elements[0].fileElement;meta.fileMd5='invalid';meta.file10MMd5=md5;
 await assert.rejects(h.downloader.download(h.row,0),/qq_media_invalid/);
 meta.fileMd5='';meta.file10MMd5='';await assert.rejects(h.downloader.download(h.row,0),/qq_media_invalid/);
 assert.equal(h.calls(),0);
});
