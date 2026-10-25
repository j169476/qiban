// Trusted short-test controller. No message text or database key enters files.
import {createPublicKey, generateKeyPairSync, sign, verify} from 'node:crypto';
import {readFile, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

export function createController() {
  const keys = generateKeyPairSync('ed25519');
  const consumed = new Set();
  return {
    publicKey: keys.publicKey.export({type:'spki', format:'der'}).toString('base64'),
    async waitAndSubmit({sky, window, root, publicKey, id, viewToken}) {
      if (!/^[a-f0-9]{32}$/.test(id)) throw Error('invalid_action');
      if (consumed.has(id)) throw Error('action_already_consumed');
      consumed.add(id);
      const server = createPublicKey({key:Buffer.from(publicKey,'base64'), type:'spki', format:'der'});
      const publish = async type => {
        const payload = Buffer.from(JSON.stringify({v:1,type,id,view_token:viewToken}));
        await writeFile(join(root,`${id}.${type}.json`),JSON.stringify({
          payload:payload.toString('base64'), signature:sign(null,payload,keys.privateKey).toString('base64')
        }),{flag:'wx'});
      };
      await publish('ready');
      const deadline = Date.now()+35000;
      while (Date.now()<deadline) {
        let raw;
        try { raw=await readFile(join(root,`${id}.grant.json`)); }
        catch(e) { if(e.code!=='ENOENT') throw e; await delay(10); continue; }
        if(raw.length>2048) throw Error('invalid_grant');
        let envelope;
        try { envelope=JSON.parse(raw.toString()); } catch { await delay(10); continue; }
        const body=Buffer.from(envelope.payload,'base64');
        if(!verify(null,body,server,Buffer.from(envelope.signature,'base64'))) throw Error('invalid_signature');
        const grant=JSON.parse(body.toString());
        if(grant.v!==1||grant.type!=='grant'||grant.id!==id||grant.view_token!==viewToken||
           !Number.isSafeInteger(grant.expires_unix_ms)||grant.expires_unix_ms>Date.now()+2000||
           Date.now()+100>=grant.expires_unix_ms) throw Error('invalid_or_expired_grant');
        // Exactly one input after the caller inspected this window and draft.
        await sky.press_key({window,key:'Return'});
        await publish('submitted');
        return await sky.get_window_state({window,include_screenshot:true});
      }
      throw Error('control_wait_timeout');
    }
  };
}
