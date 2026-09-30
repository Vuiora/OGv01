// Embedded Code-node body. Shared with the local workflow validation harness.
const parts=$input.all().map(i=>i.json).sort((a,b)=>a.part-b.part);
const root=$('保留图片并切分').first().json;
if(parts.length!==root.total||parts.some((p,i)=>p.part!==i+1||p.total!==root.total))throw new Error('翻译分批不完整，已停止导入。');
const files=[],names=new Set(),summaries=[];
const add=async(name,text,kind,extra={})=>files.push({json:{outputDir:root.outputDir,folder:root.prefix,relativePath:name,kind,...extra},binary:{data:await this.helpers.prepareBinaryData(Buffer.from(text,'utf8'),name,'text/'+(kind==='report'?'plain':'markdown'))}});
for(let chapter=1;chapter<=root.chapterCount;chapter++){
  const batch=parts.filter(p=>p.chapter===chapter);
  if(!batch.length||batch.length!==batch[0].chapterParts||batch.some((p,i)=>p.chapterPart!==i+1))throw new Error('第 '+chapter+' 章不完整。');
  const joined=batch.map(p=>p.formatted).join('\n\n');
  const heading=markdownHeadings(joined)[0];
  if(!heading||heading.offset>5)throw new Error('第 '+chapter+' 章缺少译文章节标题。');
  const title=chapterFileTitle(heading.title),name=title+'.md';
  if(names.has(name))throw new Error('章节名称重复：'+title+'。请检查提取结果。');
  names.add(name);
  const formatted=formatChapterMarkdown(joined,title),warnings=batch.flatMap(p=>p.warnings||[]);
  const meta='---\nsource: '+JSON.stringify(root.filename)+'\nchapter: '+chapter+'\ntitle: '+JSON.stringify(title)+'\nsource_chapter: '+JSON.stringify(batch[0].chapterTitle)+'\nreview: machine\nreview_notes: '+warnings.length+'\n---\n\n';
  await add(name,meta+formatted.text,'chapter',{chapter,title});
  summaries.push({chapter,title,file:name,sourceChapter:batch[0].chapterTitle,warnings,code:formatted.stats});
}
for(const asset of root.assets||[])files.push({json:{outputDir:root.outputDir,folder:root.prefix,relativePath:asset.name,kind:'image'},binary:{data:await this.helpers.prepareBinaryData(Buffer.from(asset.data,'base64'),asset.name,asset.mime)}});
await add('处理记录.json',JSON.stringify({source:root.filename,completedAt:new Date().toISOString(),chapters:summaries,excluded:'目录、前言、致谢、作者介绍、参考书目及索引等非正文部分',review:'machine'},null,2),'report');
return files;
