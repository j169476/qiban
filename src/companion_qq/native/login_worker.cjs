'use strict';
// SPDX-License-Identifier: MIT
const readline = require('node:readline');
const {LoginSession} = require('./login_session.cjs');
let nonce = null, session = null, chain = Promise.resolve();
const errors = new Set(['qq_login_state', 'qq_login_timeout', 'qq_login_failed', 'qq_native_runtime_version',
  'qq_native_catalog_invalid']);
function reply(id, value) {process.stdout.write('\nQIBAN_NATIVE:' + nonce + ':' + JSON.stringify({id, ...value}) + '\n');}
async function dispatch(request) {
  if (!request || !Number.isSafeInteger(request.id) || request.id <= 0) throw Error('invalid request');
  const {id, command, args} = request;
  if (command === 'open') {
    if (session || typeof args?.nonce !== 'string' || !/^[a-f0-9]{64}$/.test(args.nonce)) throw Error('invalid open');
    nonce = args.nonce;
  }
  try {
    let result;
    if (command === 'open') {session = new LoginSession(args.config); result = await session.open();}
    else if (!session) throw Error('qq_login_state');
    else if (command === 'login_status') result = session.state.status();
    else if (command === 'login_accounts') result = await session.accounts();
    else if (command === 'login_qr') result = session.state.request();
    else if (command === 'login_password') {
      try {
        void session.state.password(args?.account, args?.password).catch(() => session.state.fail());
        result = session.state.status();
      }
      finally {if (args) args.password = ''; }
    }
    else if (command === 'close') {
      session.close(); reply(id, {ok: true, result: {closed: true}}); setImmediate(() => process.exit(0)); return;
    } else throw Error('qq_login_state');
    reply(id, {ok: true, result});
  } catch (error) {reply(id, {ok: false, error: errors.has(error.message) ? error.message : 'qq_login_failed'});}
}
readline.createInterface({input: process.stdin, crlfDelay: Infinity}).on('line', line => {
  chain = chain.then(() => {
    if (Buffer.byteLength(line) > 262144) throw Error('request limit');
    return dispatch(JSON.parse(line));
  }).catch(() => {session?.close(); process.exit(2);});
}).on('close', () => {session?.close(); process.exit(0);});
