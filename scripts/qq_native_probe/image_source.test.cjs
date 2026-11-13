'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {registerImage}=require('../../src/companion_qq/native/image_source.cjs');
test('SDK registration reuses identical bytes without overwriting a conflicting cache file',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-image-source-'));
 const target=path.join(root,'image.png'),bytes=Buffer.from('original');
 const service={getRichMediaFilePathForGuild(info){assert.equal(info.elementType,2);assert.equal(info.needCreate,true);return target;}};
 try {
  assert.equal(registerImage(service,root,bytes,'image.png'),target);
  assert.equal(registerImage(service,root,bytes,'image.png'),target);
  fs.writeFileSync(target,'conflict');assert.throws(()=>registerImage(service,root,bytes,'image.png'),/media_path/);
  assert.equal(fs.readFileSync(target,'utf8'),'conflict');
 }finally {fs.unlinkSync(target);fs.rmdirSync(root);}
});
test('SDK paths outside the own-account cache cannot be written',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'qiban-image-source-'));
 try {for(const destination of [path.join(root,'..','escape.png'),root,'relative.png'])
  assert.throws(()=>registerImage({getRichMediaFilePathForGuild:()=>destination},root,Buffer.from('bytes'),'image.png'),/media_path/);
  assert.deepEqual(fs.readdirSync(root),[]);
 }finally{fs.rmdirSync(root);}
});
