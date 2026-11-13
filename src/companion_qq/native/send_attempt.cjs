'use strict';
// SPDX-License-Identifier: MIT
// R0 probe guard: persist uncertainty before invoking a non-idempotent transport.
const fs = require('node:fs');
function claimAttempt(file, identity) {
  let fd;
  try { fd = fs.openSync(file, 'wx'); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const record = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const key of ['account', 'target', 'marker', 'text_sha256']) {
      if (record[key] !== identity[key]) throw Error('Existing attempt belongs to a different operation');
    }
    if (record.state !== 'unknown') throw Error('Unexpected probe attempt state');
    return {claimed: false, record};
  }
  const record = {...identity, state: 'unknown', recorded_at: new Date().toISOString()};
  try { fs.writeFileSync(fd, JSON.stringify(record)); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  return {claimed: true, record};
}
module.exports = {claimAttempt};
