'use strict';
const fs=require('node:fs/promises'),path=require('node:path');
const ALLOWED_ROOT=path.resolve('D:/Obsidian/ob/OG-blog');
function inside(root,target){const rel=path.relative(root,target);return rel!==''&&!rel.startsWith('..')&&!path.isAbsolute(rel);}
async function checkedDirectory(directory){
  const resolved=path.resolve(directory),root=path.parse(resolved).root;
  let current=root;
  for(const component of resolved.slice(root.length).split(path.sep).filter(Boolean)){
    current=path.join(current,component);
    try{const stat=await fs.lstat(current);if(stat.isSymbolicLink()||!stat.isDirectory())throw new Error('输出路径包含链接或非目录：'+current);}
    catch(error){if(error.code!=='ENOENT')throw error;await fs.mkdir(current);}
  }
}
class ObsidianChapterWriter {
  description={displayName:'写入 Obsidian 章节',name:'obsidianChapterWriter',group:['output'],version:1,description:'按章节名称保存到 OG-blog 的独立文件夹，拒绝覆盖不同内容。',defaults:{name:'写入 OG-blog'},inputs:['main'],outputs:['main'],properties:[]};
  async execute(){
    const input=this.getInputData(),planned=[];
    for(let i=0;i<input.length;i++){
      const item=input[i],j=item.json;
      if(typeof j.folder!=='string'||!j.folder||/[<>:"/\\|?*]/.test(j.folder)||j.folder==='.'||j.folder==='..')throw new Error('无效的书籍输出目录');
      const base=path.resolve(j.outputDir||ALLOWED_ROOT);
      if(base!==ALLOWED_ROOT&&!inside(ALLOWED_ROOT,base))throw new Error('输出目录必须位于 OG-blog 内');
      const dir=path.resolve(base,j.folder),target=path.resolve(dir,j.relativePath||'');
      if(!inside(ALLOWED_ROOT,dir)||!inside(dir,target)||!/^.+\.(md|png|jpg|webp|json)$/i.test(target))throw new Error('输出路径超出本书目录');
      const buffer=await this.helpers.getBinaryDataBuffer(i,'data');
      planned.push({item,i,dir,target,buffer});
    }
    // Preflight all existing files before writing any content.
    for(const p of planned){
      await checkedDirectory(path.dirname(p.target));
      try{const stat=await fs.lstat(p.target);if(!stat.isFile()||stat.isSymbolicLink())throw new Error('输出目标不是普通文件');const old=await fs.readFile(p.target);if(!old.equals(p.buffer))throw new Error('目标文件已存在且内容不同，已停止以免覆盖：'+p.target);p.exists=true;}
      catch(error){if(error.code!=='ENOENT')throw error;}
    }
    const output=[];
    for(const p of planned){
      if(!p.exists)await fs.writeFile(p.target,p.buffer,{flag:'wx'});
      output.push({json:{...p.item.json,filePath:p.target,bookDirectory:p.dir,written:!p.exists},pairedItem:{item:p.i}});
    }
    return [output];
  }
}
exports.ObsidianChapterWriter=ObsidianChapterWriter;
