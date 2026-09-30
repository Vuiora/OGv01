/* Chapter boundaries and conservative code layout; shared by n8n and migration. */
function plainHeading(text) {
  return text.replace(/^#+\s*/, '').replace(/\*\*|==|`/g, '').replace(/\\([_()[\]])/g, '$1').trim();
}
function headingKey(text) {
  return plainHeading(text).replace(/^(?:chapter\s+)?\d+\s+/i, '').replace(/\s+\d+$/, '').replace(/[^\p{L}\p{N}]/gu, '').toLowerCase();
}
function markdownHeadings(text) {
  const result=[]; let offset=0,fence=null;
  for(const line of text.split('\n')) {
    const f=line.match(/^\s*(`{3,}|~{3,})/);
    if(f){if(!fence) fence=f[1][0]; else if(f[1][0]===fence) fence=null;}
    if(!fence && !f) {const m=line.match(/^(#{1,7})\s+(.+)$/);if(m)result.push({offset,end:offset+line.length,level:m[1].length,title:plainHeading(m[2]),line});}
    offset+=line.length+1;
  }
  return result;
}
function excludedHeading(title) {
  return /^(?:contents(?: at a glance)?|table of contents|foreword|preface|(?:.* )?acknowledg(?:e)?ments|about the author|bibliography|index|references|目录|内容一览|前言|序言|序|致谢|关于作者|作者简介|参考书目|参考文献|索引)$/i.test(plainHeading(title));
}
function cleanChapterBody(text) {
  let fence=null;
  return text.split('\n').filter(line=>{
    const f=line.match(/^\s*(`{3,}|~{3,})/);if(f){if(!fence)fence=f[1][0];else if(fence===f[1][0])fence=null;return true;}
    if(fence)return true;
    return !/^#{1,7}\s+\d+\s*$/.test(line) && !/^(?:本页(?:有意|特意)留白[。.]?|此页故意留白[。.]?|This page intentionally left blank\.?|www\.it-ebooks\.info)$/i.test(line.trim());
  }).join('\n').trim();
}
function chapterPlan(text) {
  const heads=markdownHeadings(text),toc=heads.find(h=>/^(?:table of contents|目录)$/i.test(h.title));
  let starts=[];
  if(toc) {
    const entries=[];let expected=1;
    for(const h of heads.filter(h=>h.offset>toc.offset)) {
      if(/^(?:foreword|preface|序言|前言)$/i.test(h.title))break;
      const m=h.title.match(/^(?:chapter\s+)?(\d+)\s+(.+?)(?:\s+(\d+))?$/i);
      if(m && Number(m[1])===expected){entries.push({...h,key:headingKey(h.title)});expected++;}
    }
    if(entries.length>=2){
      let cursor=entries.at(-1).end;
      for(const entry of entries) {
        const h=heads.find(h=>h.offset>cursor && headingKey(h.title)===entry.key);
        if(!h)throw new Error('无法定位正文的章节：'+entry.title+'。请检查提取结果或限定正文页码。');
        starts.push(h);cursor=h.end;
      }
    }
  }
  if(!starts.length) {
    const numbered=heads.filter(h=>/^(?:chapter\s+\d+\b|第[一二三四五六七八九十百零\d]+章)/i.test(h.title));
    if(numbered.length>=2) starts=numbered;
    else {
      const front=heads.filter(h=>/^(?:contents(?: at a glance)?|table of contents|foreword|preface|(?:.* )?acknowledg(?:e)?ments|about the author|目录|内容一览|前言|序言|致谢|作者简介)$/i.test(h.title));
      const cutoff=front.at(-1)?.end??-1;
      const eligible=heads.filter(h=>h.offset>cutoff&&!excludedHeading(h.title)&&!/^\d+$/.test(h.title)&&h.level<=3);
      if(!eligible.length)throw new Error('未识别到正文章节标题，请限定正文页码或检查标题提取结果。');
      let level=Math.min(...eligible.map(h=>h.level));
      // A single book title followed by several chapter headings is a container.
      if(eligible.filter(h=>h.level===level).length===1 && eligible.filter(h=>h.level===level+1).length>=2) level++;
      starts=eligible.filter(h=>h.level===level);
    }
  }
  const back=heads.find(h=>h.offset>starts[0].offset&&/^(?:bibliography|index|references|参考书目|参考文献|索引)$/i.test(h.title)&&h.level<=Math.max(2,starts[0].level));
  starts=starts.filter(h=>!excludedHeading(h.title)&&(!back||h.offset<back.offset));
  return starts.map((h,i)=>{
    const end=starts[i+1]?.offset??back?.offset??text.length;
    return {number:i+1,title:h.title.replace(/^(?:chapter\s+)?\d+\s+/i,''),start:h.offset,end,text:cleanChapterBody(text.slice(h.offset,end))};
  });
}
function chapterFileTitle(title) {
  const substitutions={'/':'／','\\':'＼',':':'：','?':'？','*':'＊','"':'＂','<':'＜','>':'＞','|':'｜','[':'［',']':'］','#':'＃','^':'＾'};
  const name=plainHeading(title).replace(/[<>:"/\\|?*\[\]#^]/g,c=>substitutions[c]).replace(/[\x00-\x1f]/g,'').replace(/[. ]+$/,'').slice(0,100);
  if(!name||/^(?:con|prn|aux|nul|com\d|lpt\d)$/i.test(name))throw new Error('无效章节文件名：'+title);
  return name;
}
function inferCodeLanguage(code,existing='') {
  if(existing.trim())return existing.trim().split(/\s/)[0].toLowerCase();
  const t=code.trim();
  if(/^(?:diff --git|@@ |--- a\/|\+\+\+ b\/)/m.test(t))return 'diff';
  if(/^(?:\$ |# (?:make|echo|cat|insmod|modprobe)|#!.*(?:bash|sh)|(?:make|git|tar|patch|insmod|rmmod|modprobe|depmod|cat|echo|cd|ls|ps|indent|diff|zcat)\s)/.test(t))return 'bash';
  if(/^(?:mov[lqbw]?|and[lqbw]?|push[lqbw]?|pop[lqbw]?|add[lqbw]?)\s+[^\n]*%[a-z]/i.test(t))return 'asm';
  if(/^(?:obj-[my]|obj-\$|[A-Z_]+\s*[+:]?=)/m.test(t)&&!/[;{}]/.test(t))return 'makefile';
  if(/^(?:config\s+\w+|menuconfig\s+\w+|depends on\s|select\s)/.test(t))return 'kconfig';
  if(/^(?:Trace|Call Trace|Oops|BUG:|CPU\d|[0-9a-f]{8}-[0-9a-f]{8}|\|--|Atomic Integer Operation Description)/i.test(t))return 'text';
  if(/^[{}\s.]+$/.test(t)||/^(?:\w+_t\b|\*\s+(?:FIXME|TODO):)/.test(t))return 'c';
  if(/(?:#\s*(?:include|define|if|ifdef|ifndef|endif)\b|\b(?:struct|enum|typedef|unsigned|static|extern|void|int|char|long|return|if|while|for|switch)\b|->|;|\/\*|\b\w+\([^\n]*\))/m.test(t))return 'c';
  return 'text';
}
function formatCWhitespace(code) {
  const original=code;
  code=code.replace(/\s+(?=#\s*(?:define|include|ifdef|ifndef|endif)\b)/g,'\n');
  // Lex comments and quoted literals as indivisible tokens. Never rewrite them.
  const raw=code.match(/\/\*[\s\S]*?\*\/|\/\/[^\n]*|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|^\s*#(?:[^\n\\]|\\[\s\S])*|\s+|[^\s{}();"']+|[\s\S]/gm)||[];
  const lines=[];let line='',depth=0,paren=0,pending='',previous='';
  const flush=()=>{if(line.trim())lines.push('    '.repeat(Math.max(0,depth))+line.trim());line='';};
  for(let i=0;i<raw.length;i++){
    const t=raw[i];if(/^\s+$/.test(t)){pending=t;continue;}
    if(previous==='}'&&!/^[;,)]$/.test(t)&&!/^else\b/.test(t))flush();
    if(previous.endsWith('*/')&&!t.startsWith('/*'))flush();
    if(pending.includes('\n') && line && !paren)flush();
    if(t==='}') {flush();depth=Math.max(0,depth-1);line='}';}
    else if(t==='{'){line+=(line&&!/\s$/.test(line)?' ':'')+'{';flush();depth++;}
    else if(t===';'){line+=';';if(!paren)flush();}
    else if(/^\s*#/.test(t)){flush();lines.push(t.trim());}
    else {
      const gap=line&&pending&&!/^[),;]$/.test(t)&&!/[\s(]$/.test(line)?' ':'';
      line+=gap+t;
      if(t==='(')paren++;if(t===')')paren=Math.max(0,paren-1);
      if(t.startsWith('//'))flush();
    }
    pending='';previous=t;
  }
  flush();const result=lines.join('\n');
  if(result.replace(/\s/g,'')!==code.replace(/\s/g,''))throw new Error('代码格式化改变了非空白字符，已停止。');
  // Literal/comment bytes must remain exact (leading indentation is outside tokens).
  const literals=s=>s.match(/\/\*[\s\S]*?\*\/|\/\/[^\n]*|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'/g)||[];
  if(JSON.stringify(literals(result))!==JSON.stringify(literals(original)))return original.trim();
  return result;
}
function formatCodeBlock(code,language) {
  if(language==='c'||language==='cpp')return formatCWhitespace(code);
  if(language==='asm')return code.trim().replace(/\s+(?=(?:mov[lqbw]?|and[lqbw]?|push[lqbw]?|pop[lqbw]?|add[lqbw]?)\s)/g,'\n').split('\n').map(l=>'    '+l.trim()).join('\n');
  if(language==='bash')return code.trim().replace(/\s+(?=\$\s+(?:make|git|tar|patch|insmod|rmmod|modprobe|depmod|cat|echo|cd|ls)\b)/g,'\n');
  if(language==='makefile')return code.trim().replace(/\s+(?=(?:[\w-]+\s*[+:]?=))/g,'\n');
  return code.trimEnd();
}
function formatChapterMarkdown(text,title) {
  const stats={blocks:0,languages:{},addedFences:0};
  const output=[],lines=cleanChapterBody(text).split('\n');let firstHeading=true;
  const emitCode=(body,info='')=>{
    // Docling sometimes places explanatory sentences inside a code item.
    // Move only recognizable prose spans outside fences; preserve their text.
    const prose=/(?:^Finally, let's look[\s\S]*?exist\):|As with adding a process[\s\S]*?__dequeue_entity\(\)\s*:|^In a driver,[\s\S]*?request_irq\(\)\s*:|Operations are all simple:|If you ever need[\s\S]*?atomic_read\(\)\s*:|^tion of kernel timers,[\s\S]*?it:|Then you access it as|^You can use the system[\s\S]*?comments:|For more information, see Documentation\/kernel-doc-nano-HOWTO\.txt\s*\.|If it is compressed with GNU zip, run|Or for postscript)/g;
    const matches=[...body.matchAll(prose)];
    if(matches.length){let at=0;for(const m of matches){const before=body.slice(at,m.index);if(before.trim())emitCode(before,info);output.push('',m[0],'');at=m.index+m[0].length;}if(body.slice(at).trim())emitCode(body.slice(at),info);return;}
    const lang=inferCodeLanguage(body,info),formatted=formatCodeBlock(body,lang);
    const fence='`'.repeat(Math.max(3,1+Math.max(0,...(formatted.match(/`+/g)||[]).map(s=>s.length))));
    output.push(fence+lang,formatted,fence);stats.blocks++;stats.languages[lang]=(stats.languages[lang]||0)+1;
  };
  for(let i=0;i<lines.length;i++){
    const line=lines[i],f=line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if(f){
      let body=[];const close=new RegExp('^\\s*'+f[1][0]+'{'+f[1].length+',}\\s*$');i++;for(;i<lines.length&&!close.test(lines[i]);i++)body.push(lines[i]);if(i===lines.length)throw new Error('未闭合的代码块');
      // Rejoin adjacent fragments separated only by a PDF page or API chunk boundary.
      while(true){let next=i+1;while(next<lines.length&&!lines[next].trim())next++;const nf=lines[next]?.match(/^\s*(`{3,}|~{3,})(.*)$/);if(!nf)break;
        let end=next+1;const nc=new RegExp('^\\s*'+nf[1][0]+'{'+nf[1].length+',}\\s*$');while(end<lines.length&&!nc.test(lines[end]))end++;
        if(end===lines.length)throw new Error('未闭合的代码块');const extra=lines.slice(next+1,end).join('\n');
        if(inferCodeLanguage(body.join('\n'),f[2])!==inferCodeLanguage(extra,nf[2]))break;
        body.push('',extra);i=end;
      }
      emitCode(body.join('\n'),f[2]);continue;
    }
    if(/^#{1,7}\s+/.test(line)){
      if(firstHeading){output.push('# '+title);firstHeading=false;}
      else {const h=line.match(/^(#+)\s+(.*)$/);output.push('#'.repeat(h[1].length>=5?4:Math.max(2,h[1].length-1))+' '+h[2]);}
      continue;
    }
    if(line.trim() && !line.startsWith('|') && !line.startsWith('![')){
      let end=i+1;while(end<lines.length&&lines[end].trim()&&!/^(?:#{1,7}\s|`{3,}|~{3,})/.test(lines[end]))end++;
      const paragraph=lines.slice(i,end).join('\n');
      const code=paragraph.replace(/\*\*/g,'').replace(/\\([_<>])/g,'$1').replace(/&lt;/g,'<').replace(/&gt;/g,'>');
      if(!/[\u3400-\u9fff]/.test(code)&&/^(?:\s*(?:\/\*[\s\S]*?\*\/|\/\/[^\n]*)\s*)*(?:struct\s|(?:static\s+)?(?:unsigned\s+)?(?:int|char|void|long)\s|[A-Za-z_]\w*(?:->\w+)?\s*(?:\(|=))/.test(code)&&/[;{}]\s*(?:\/\*[\s\S]*?\*\/)?\s*$/.test(code)&&!/^[-*]\s/m.test(code)){
        emitCode(code,'c');stats.addedFences++;i=end-1;continue;
      }
    }
    output.push(line.replace(/^(- )n\s+/,'$1'));
  }
  if(firstHeading)output.unshift('# '+title,'');
  return {text:output.join('\n').trim()+'\n',stats};
}
if(typeof module!=='undefined')module.exports={plainHeading,headingKey,markdownHeadings,excludedHeading,cleanChapterBody,chapterPlan,chapterFileTitle,inferCodeLanguage,formatCWhitespace,formatCodeBlock,formatChapterMarkdown};
