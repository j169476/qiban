'use strict';
// SPDX-License-Identifier: MIT
// Register original image bytes at the pinned QQ SDK's own-account media path.
const fs=require('node:fs'), path=require('node:path'), crypto=require('node:crypto');
const check=ok=>{if(!ok)throw Error('qq_media_path');};
function registerMedia(service, cacheRoot, bytes, name, elementType) {
  check([2,3].includes(elementType));
  check(typeof cacheRoot==='string' && path.isAbsolute(cacheRoot));
  const root=path.resolve(cacheRoot);
  fs.mkdirSync(root,{recursive:true});
  check(!fs.lstatSync(root).isSymbolicLink() && fs.realpathSync(root)===root);
  const md5=crypto.createHash('md5').update(bytes).digest('hex');
  const returned=service.getRichMediaFilePathForGuild({md5HexStr:md5,fileName:name,
    elementType,elementSubType:0,thumbSize:0,needCreate:true,downloadType:1,file_uuid:''});
  check(typeof returned==='string' && path.isAbsolute(returned));
  const file=path.resolve(returned),relative=path.relative(root,file);
  check(relative && !relative.startsWith('..'+path.sep) && relative!=='..' && !path.isAbsolute(relative));
  let directory=root;
  for(const segment of path.relative(root,path.dirname(file)).split(path.sep).filter(Boolean)) {
    directory=path.join(directory,segment);
    if(!fs.existsSync(directory))fs.mkdirSync(directory);
    check(fs.lstatSync(directory).isDirectory() && !fs.lstatSync(directory).isSymbolicLink()
      && fs.realpathSync(directory)===directory);
  }
  if(fs.existsSync(file)) {
    const stat=fs.lstatSync(file);
    check(stat.isFile() && !stat.isSymbolicLink() && fs.realpathSync(file)===file && stat.size===bytes.length);
    check(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')===
      crypto.createHash('sha256').update(bytes).digest('hex'));
  } else fs.writeFileSync(file,bytes,{flag:'wx'});
  return file;
}
const registerImage=(service,root,bytes,name)=>registerMedia(service,root,bytes,name,2);
const registerFile=(service,root,bytes,name)=>registerMedia(service,root,bytes,name,3);
module.exports={registerImage,registerFile};
