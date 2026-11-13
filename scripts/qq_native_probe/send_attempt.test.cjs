'use strict';
// SPDX-License-Identifier: MIT
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {spawnSync} = require('node:child_process');
const {claimAttempt} = require('./send_attempt.cjs');
const identity = {account: 'synthetic-bot', target: 'synthetic-peer', marker: 'case-1', text_sha256: 'synthetic-digest'};
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qiban-attempt-'));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  return path.join(root, 'attempt.json');
}
test('uncertainty is on disk before the transport can be invoked', t => {
  const file = fixture(t);
  assert.equal(claimAttempt(file, identity).claimed, true);
  assert.equal(JSON.parse(fs.readFileSync(file)).state, 'unknown');
});
test('process restart cannot reclaim an unknown attempt', t => {
  const file = fixture(t); claimAttempt(file, identity);
  const child = spawnSync(process.execPath, ['-e',
    "const {claimAttempt}=require(process.argv[1]); process.exit(claimAttempt(process.argv[2],JSON.parse(process.argv[3])).claimed?9:0)",
    require.resolve('./send_attempt.cjs'), file, JSON.stringify(identity)], {encoding:'utf8'});
  assert.equal(child.status, 0, child.stderr);
  assert.equal(claimAttempt(file, identity).claimed, false);
});
test('a different recipient or payload cannot reuse the attempt', t => {
  const file = fixture(t); claimAttempt(file, identity);
  for (const key of ['account','target','marker','text_sha256']) {
    assert.throws(() => claimAttempt(file, {...identity, [key]:'different'}), /different operation/);
  }
});
test('a damaged attempt blocks sending instead of becoming a new attempt', t => {
  const file = fixture(t); fs.writeFileSync(file, '{"state":');
  assert.throws(() => claimAttempt(file, identity), SyntaxError);
  assert.equal(fs.readFileSync(file,'utf8'), '{"state":');
});
