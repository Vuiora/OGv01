"use strict";
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const palette = ["#719969", "#7097bc", "#b6a069", "#9f89ac", "#b18876", "#6e9e9d"];
const fills = ["#f0f5e9", "#edf3f9", "#faf6e9", "#f4eff7", "#faf0eb", "#ecf6f4"];
const labels = {part_of:"属于", prerequisite:"是前提", explains:"解释", causes:"导致", contrasts:"对比", applies_to:"应用于", depends_on:"依赖", related:"关联"};
const statuses = {uploaded:"已上传",queued:"等待调度",parse:"解析中",extract:"提取中",relate:"分析中",render:"生成中",waiting:"等待下一阶段",completed:"已完成",failed:"失败"};
let graph = null, documentText = null, apiKey = "", currentJob = null, pollTimer = null, toastTimer = null;
let sectionFilter = "", selected = null, nodePoints = new Map(), view = {x:0,y:0,scale:1}, dragged = null;
let graphWidth = 700, graphHeight = 440, modelConfigured = false, pendingFile = null, authRequired = false;
let focusId = null, displayedLayout = null, reanalysisRunning = false;

function element(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if(className) e.className=className; return e; }
function svgElement(tag, attrs={}) { const e = document.createElementNS(NS,tag); for(const [key,value] of Object.entries(attrs)) e.setAttribute(key,String(value)); return e; }
function toast(message) { $("toast").textContent=message; $("toast").hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$("toast").hidden=true,5500); }
async function api(path, options={}) { const response = await fetch(path,{...options,headers:{...(apiKey?{"X-API-Key":apiKey}:{}),...options.headers}}); if(!response.ok){ let detail; try{detail=(await response.json()).detail;}catch{detail=`HTTP ${response.status}`;} throw new Error(typeof detail==="string"?detail:JSON.stringify(detail)); } return response; }
function jsonOptions(method,body) { return {method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}; }
function openDialog(id) { if(!$(id).open) $(id).showModal(); }
for(const button of document.querySelectorAll(".close-dialog")) button.onclick=()=>button.closest("dialog").close();
for(const [type,label] of Object.entries(labels)) { const option=element("option",label);option.value=type;$("type-filter").append(option); }

async function loadDemo() { clearTimeout(pollTimer); currentJob=null; $("job-progress").hidden=true; try{setGraph(await (await api("/api/demo")).json());}catch(error){toast(error.message);} }
function setGraph(value) {
  graph=value; documentText=null; selected=null; sectionFilter=""; $("search").value="";$("type-filter").value="";$("confidence").value=0;$("confidence-value").textContent="全部";
  focusId=null;$("view-mode").value="backbone";$("reanalyze").hidden=!!graph.demo;
  $("document-name").textContent=graph.title;$("document-meta").textContent=graph.demo?"内置演示文档 · 人工整理":`${graph.document.converter} · 解析完成`;
  $("concept-count").textContent=graph.concepts.length;$("part-count").textContent=graph.sections.length;
  $("graph-badge").textContent=graph.demo?"内置示例":"文档分析";
  $("canvas-label").textContent=graph.demo?"内置示例 · 非本次模型结果":"完整文档上下文 · 原文证据可追溯";
  $("graph-subtitle").textContent=graph.extraction_policy?.mode==="document_key_concepts"?`全文筛选 ${graph.extraction_policy.candidate_count} 个候选 → ${graph.concepts.length} 个重要概念` : "选择概念或连线查看原文依据；旧任务可重新提取重要概念";
  updateCounts();renderSections();layout();
  if(graph.concepts.length)titleBlock("READING / 阅读图谱","从主干开始","默认展示连接各概念的主干，补充关系可切换“全部关系”查看。点击概念可阅读重要性理由、原文证据或聚焦其直接联系。主干只用于简化显示，不代表其他关系无效。");else showEmptyDetail();
}
function updateCounts(){ const visible=graph.relations.filter(e=>e.review!=="rejected");$("relation-count").textContent=visible.length;$("relation-summary").textContent=`${visible.filter(e=>e.review==="unreviewed").length} 条待复核`; }
function renderSections() {$("section-list").replaceChildren();$("section-count").textContent=graph.sections.length;graph.sections.forEach((section,index)=>{ const button=element("button",undefined,"section-item"+(sectionFilter===section.id?" active":""));const dot=element("span",undefined,"section-dot");dot.style.background=palette[index%palette.length];button.append(dot,element("span",section.title,"section-title"),element("small",graph.concepts.filter(c=>c.section_ids.includes(section.id)).length));button.onclick=()=>{sectionFilter=sectionFilter===section.id?"":section.id;renderSections();renderGraph();};$("section-list").append(button);}); }

function layout() {
  if(!graph)return;
  const rect=$("graph-canvas").getBoundingClientRect();graphWidth=rect.width;graphHeight=rect.height;
  updateGeometry();
  fit();renderGraph();
}
function visibleGraph(){
  if(!graph)return {nodes:[],edges:[]};
  const type=$("type-filter").value,confidence=Number($("confidence").value)/100,mode=$("view-mode").value;
  let nodes=graph.concepts.filter(node=>!sectionFilter||node.section_ids.includes(sectionFilter));
  const ids=new Set(nodes.map(node=>node.id));
  let edges=graph.relations.filter(edge=>edge.review!=="rejected"&&ids.has(edge.source)&&ids.has(edge.target)&&(!type||edge.type===type)&&edge.confidence>=confidence);
  if(mode==="focus"){
    edges=edges.filter(edge=>edge.source===focusId||edge.target===focusId);
    const neighborhood=new Set([focusId,...edges.flatMap(edge=>[edge.source,edge.target])]);
    nodes=nodes.filter(node=>neighborhood.has(node.id));
  }else if(mode==="backbone"){
    const backbone=new Set(graph.layout.backbone_ids);
    edges=edges.filter(edge=>backbone.has(edge.id)||selected?.kind==="edge"&&selected.id===edge.id);
  }
  return {nodes,edges};
}
function updateGeometry(){
  if(!graph)return;
  displayedLayout=graph.layout;
  if($("view-mode").value==="focus"){
    const {nodes,edges}=visibleGraph(),neighbors=nodes.filter(node=>node.id!==focusId);
    const positions=Object.fromEntries(neighbors.map((node,index)=>[node.id,{x:392,y:100+index*100}]));
    if(nodes.some(node=>node.id===focusId))positions[focusId]={x:100,y:100+Math.max(0,neighbors.length-1)*50};
    const routes={};
    for(const edge of edges){
      const source=positions[edge.source],target=positions[edge.target],side=source.x<target.x?1:-1;
      routes[edge.id]=[[source.x+88*side,source.y],[246,source.y],[246,target.y],[target.x-88*side,target.y]];
    }
    displayedLayout={...graph.layout,positions,routes};
  }
  nodePoints=new Map(graph.concepts.filter(node=>displayedLayout.positions[node.id]).map(node=>[node.id,{...displayedLayout.positions[node.id],node}]));
}
function fit(){
  updateGeometry();
  const visible=visibleGraph(),points=visible.nodes.map(node=>nodePoints.get(node.id));
  if(!points.length){view={x:0,y:0,scale:1};transform();return;}
  const routePoints=visible.edges.flatMap(edge=>(displayedLayout.routes[edge.id]||[]).map(([horizontal,vertical])=>({x:horizontal,y:vertical})));
  const allPoints=[...points,...routePoints];
  const minX=Math.min(...allPoints.map(point=>point.x))-100,maxX=Math.max(...allPoints.map(point=>point.x))+100;
  const minY=Math.min(...allPoints.map(point=>point.y))-45,maxY=Math.max(...allPoints.map(point=>point.y))+45;
  const scale=Math.min(1.25,(graphWidth-32)/(maxX-minX),(graphHeight-70)/(maxY-minY));
  view={scale,x:(graphWidth-(minX+maxX)*scale)/2,y:(graphHeight-30-(minY+maxY)*scale)/2};transform();
}
function transform(){$("viewport").setAttribute("transform",`translate(${view.x},${view.y}) scale(${view.scale})`);$("zoom-value").textContent=Math.round(view.scale*100)+"%";}
function zoom(factor,px=graphWidth/2,py=graphHeight/2){const old=view.scale;view.scale=Math.max(.12,Math.min(3,view.scale*factor));view.x=px-(px-view.x)*view.scale/old;view.y=py-(py-view.y)*view.scale/old;transform();}
function renderGraph(){
  if(!graph)return;$("nodes").replaceChildren();$("links").replaceChildren();
  updateGeometry();
  const search=$("search").value.trim().toLocaleLowerCase(),{nodes,edges}=visibleGraph();
  const matching=new Set(nodes.filter(node=>(node.label+" "+node.definition).toLocaleLowerCase().includes(search)).map(node=>node.id));
  const backbone=new Set(graph.layout.backbone_ids),activeNodes=new Set(),activeEdges=new Set();
  if(selected?.kind==="node"){
    activeNodes.add(selected.id);
    for(const edge of edges)if(edge.source===selected.id||edge.target===selected.id){activeNodes.add(edge.source);activeNodes.add(edge.target);activeEdges.add(edge.id);}
  }else if(selected?.kind==="edge"){
    const edge=graph.relations.find(relation=>relation.id===selected.id);
    if(edge){activeNodes.add(edge.source);activeNodes.add(edge.target);activeEdges.add(edge.id);}
  }
  $("visible-relations").textContent=`显示 ${edges.length} / ${graph.relations.filter(edge=>edge.review!=="rejected").length} 条关系`;
  $("graph").classList.toggle("show-edge-labels",$("show-edge-labels").checked);
  $("canvas-label").textContent=$("view-mode").value==="backbone"?"主干视图 · 补充关系可展开 · 孤立概念保留":$("view-mode").value==="focus"?"局部聚焦 · 仅显示所选概念的直接联系":"全部关系 · 虚线表示补充联系";
  $("empty-graph").hidden=!!nodes.length;$("graph").setAttribute("viewBox",`0 0 ${graphWidth} ${graphHeight}`);
  for(const edge of edges){
    const source=nodePoints.get(edge.source),target=nodePoints.get(edge.target),route=displayedLayout.routes[edge.id];
    if(!source||!target||!route)continue;
    const path=route.map(([horizontal,vertical],index)=>`${index?"L":"M"}${horizontal},${vertical}`).join(" ");
    const group=svgElement("g",{class:"graph-link"+(backbone.has(edge.id)?"":" supplementary")+(activeEdges.has(edge.id)?" connected":"")+(selected?.kind==="edge"&&selected.id===edge.id?" selected":""),"data-edge":edge.id,tabindex:0,role:"button","aria-label":`${source.node.label} ${labels[edge.type]} ${target.node.label}`});
    group.classList.toggle("dimmed",!!(search&&!matching.has(edge.source)&&!matching.has(edge.target)||selected&&!activeEdges.has(edge.id)));
    group.append(svgElement("path",{d:path,class:"edge-hit"}));
    const line=svgElement("path",{d:path,class:"edge-line"});
    if(!["related","contrasts"].includes(edge.type))line.setAttribute("marker-end","url(#arrow)");
    group.append(line);
    const penultimate=route[route.length-2],last=route[route.length-1];
    const text=svgElement("text",{x:(penultimate[0]+last[0])/2,y:(penultimate[1]+last[1])/2-8,"text-anchor":"middle"});
    text.textContent=labels[edge.type];group.append(text);
    group.onclick=()=>selectEdge(edge.id);group.onkeydown=event=>{if(event.key==="Enter")selectEdge(edge.id);};$("links").append(group);
  }
  for(const node of nodes){
    const point=nodePoints.get(node.id),sectionIndex=Math.max(0,graph.sections.findIndex(section=>section.id===node.section_ids[0])),color=sectionIndex%palette.length;
    const group=svgElement("g",{transform:`translate(${point.x},${point.y})`,class:"graph-node"+(selected?.kind==="node"&&selected.id===node.id?" selected":""),"data-node":node.id,tabindex:0,role:"button","aria-label":node.label});
    group.classList.toggle("dimmed",!!(search&&!matching.has(node.id)||selected&&!activeNodes.has(node.id)));
    group.append(svgElement("rect",{x:-88,y:-28,width:176,height:56,rx:12,fill:fills[color],stroke:palette[color],"stroke-width":1}));
    const chars=Array.from(node.label),lines=chars.length<=12?[node.label]:[chars.slice(0,12).join(""),chars.slice(12,23).join("")+(chars.length>23?"…":"")];
    const text=svgElement("text",{"text-anchor":"middle"});
    lines.forEach((line,index)=>{const span=svgElement("tspan",{x:0,y:5+index*18-(lines.length>1?9:0)});span.textContent=line;text.append(span);});
    group.append(text);const title=svgElement("title");title.textContent=node.label;group.append(title);
    group.onkeydown=event=>{if(event.key==="Enter")selectNode(node.id);};$("nodes").append(group);
  }
  transform();
}
$("graph").addEventListener("pointerdown",event=>{if(event.button!==0)return;const node=event.target.closest("[data-node]");if(event.target.closest("[data-edge]"))return;dragged={id:node?.dataset.node||null,startX:event.clientX,startY:event.clientY,lastX:event.clientX,lastY:event.clientY,moved:false};$("graph").setPointerCapture(event.pointerId);});
$("graph").addEventListener("pointermove",event=>{if(!dragged)return;const dx=event.clientX-dragged.lastX,dy=event.clientY-dragged.lastY;dragged.moved ||= Math.hypot(event.clientX-dragged.startX,event.clientY-dragged.startY)>4;if(!dragged.id){view.x+=dx;view.y+=dy;transform();}dragged.lastX=event.clientX;dragged.lastY=event.clientY;});
$("graph").addEventListener("pointerup",()=>{if(dragged?.id&&!dragged.moved)selectNode(dragged.id);dragged=null;});$("graph").addEventListener("pointercancel",()=>dragged=null);
$("graph").addEventListener("wheel",event=>{event.preventDefault();const rect=$("graph").getBoundingClientRect();zoom(event.deltaY>0?.92:1.08,event.clientX-rect.left,event.clientY-rect.top);},{passive:false});
$("zoom-in").onclick=()=>zoom(1.2);$("zoom-out").onclick=()=>zoom(1/1.2);$("zoom-fit").onclick=fit;$("reset-layout").onclick=layout;
$("search").oninput=renderGraph;$("type-filter").onchange=renderGraph;$("confidence").oninput=()=>{$("confidence-value").textContent=Number($("confidence").value)?`${$("confidence").value}%`:"全部";renderGraph();};
$("view-mode").onchange=()=>{if($("view-mode").value==="focus"){focusId=selected?.kind==="node"?selected.id:focusId||graph?.concepts[0]?.id;if(focusId)selectNode(focusId);}else selected=null;renderGraph();fit();};
$("show-edge-labels").onchange=renderGraph;
let resizeTimer;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(layout,160);}).observe($("graph-canvas"));

function titleBlock(type,title,description){const panel=$("evidence-content");panel.replaceChildren();const kicker=element("div",undefined,"detail-type");kicker.append(element("span"),document.createTextNode(type));panel.append(kicker,element("h3",title,"detail-title"),element("p",description,"detail-description"));return panel;}
function addQuotes(panel,evidence){const heading=element("div","原文依据","detail-section-title");heading.append(element("small",`${evidence.length} 处引用`));panel.append(heading);for(const item of evidence){const quote=element("div",undefined,"quote");quote.append(element("p",item.quote));const link=element("button",`${item.section} · 第 ${item.line_start}–${item.line_end} 行 ↗`);link.onclick=()=>showSource(item);quote.append(link);panel.append(quote);}}
function selectNode(id){
  selected={kind:"node",id};const node=graph.concepts.find(concept=>concept.id===id);if(!node)return;
  if($("view-mode").value==="focus")focusId=id;
  const panel=titleBlock("CONCEPT / 知识点",node.label,node.definition),tags=element("div",undefined,"detail-tags");
  node.section_ids.forEach(sectionId=>tags.append(element("span",graph.sections.find(section=>section.id===sectionId)?.title||sectionId,"tag")));
  panel.append(tags);
  if(node.importance_reason)panel.append(element("p",`为何重要：${node.importance_reason}`,"importance-reason"));
  const focusButton=element("button","◎ 聚焦此概念的直接联系","button secondary focus-concept");
  focusButton.onclick=()=>{focusId=id;$("view-mode").value="focus";renderGraph();fit();};panel.append(focusButton);
  addQuotes(panel,node.evidence);
  const edges=graph.relations.filter(edge=>edge.review!=="rejected"&&(edge.source===id||edge.target===id));
  const heading=element("div","关联知识点","detail-section-title");heading.append(element("small",`${edges.length} 条关系`));panel.append(heading);
  if(!edges.length)panel.append(element("p","文档中尚未找到可靠关系，保留为独立知识点。","detail-description"));
  for(const edge of edges){
    const target=graph.concepts.find(concept=>concept.id===(edge.source===id?edge.target:edge.source));
    const button=element("button",undefined,"related-button");
    button.append(element("span",target?.label||""),element("small",`${["related","contrasts"].includes(edge.type)?"↔":edge.source===id?"→":"←"} ${labels[edge.type]}`));
    button.onclick=()=>selectEdge(edge.id);panel.append(button);
  }
  panel.append(element("div","点击连线可查看关系方向、原文依据，并确认或否决该关系。","proof-note"));renderGraph();
  if($("view-mode").value==="focus")fit();
}
function selectEdge(id){selected={kind:"edge",id};const edge=graph.relations.find(e=>e.id===id);const source=graph.concepts.find(n=>n.id===edge.source),target=graph.concepts.find(n=>n.id===edge.target);const symmetric=["related","contrasts"].includes(edge.type);const panel=titleBlock("RELATION / 概念关系",`${source.label} ${symmetric?"↔":"→"} ${target.label}`,edge.explanation);const tags=element("div",undefined,"detail-tags");tags.append(element("span",labels[edge.type],"tag"),element("span",`模型自评 ${Math.round(edge.confidence*100)}%`,"tag"),element("span",{unreviewed:"待人工复核",accepted:"已确认",rejected:"已否决"}[edge.review],"tag"));panel.append(tags);addQuotes(panel,edge.evidence);const buttons=element("div",undefined,"review-buttons");for(const [status,title] of [["accepted","✓ 确认关系"],["rejected","× 否决关系"],["unreviewed","重置"]]){const button=element("button",title,edge.review===status?"active":"");button.disabled=graph.demo;button.onclick=()=>reviewEdge(edge,status);buttons.append(button);}panel.append(buttons,element("div",graph.demo?"示例由人工整理，仅用于展示。真实分析完成后可在这里复核关系。":"证据校验只说明引用存在且覆盖两个概念，并不证明关系语义正确。模型自评不等于统计概率。","proof-note"));renderGraph();}
async function reviewEdge(edge,status){try{await api(`/api/jobs/${graph.job_id}/relations/${edge.id}`,jsonOptions("PATCH",{status}));graph=await(await api(`/api/jobs/${graph.job_id}/graph`)).json();updateCounts();layout();selectEdge(edge.id);toast("复核结果已保存，主干已更新");}catch(error){toast(error.message);}}
function showEmptyDetail(){titleBlock("DOCUMENT / 文档","暂无知识点","此文档没有通过原文证据校验的知识点。可以查看校验记录，或调整文档后重新分析。");}
async function showSource(evidence=null){try{if(documentText===null)documentText=await (await api(graph.demo?"/api/demo/document":`/api/jobs/${graph.job_id}/document`)).text();const pre=$("source-content");pre.replaceChildren();if(evidence){const chars=Array.from(documentText);pre.append(document.createTextNode(chars.slice(0,evidence.start).join("")));const mark=element("mark",chars.slice(evidence.start,evidence.end).join(""));pre.append(mark,document.createTextNode(chars.slice(evidence.end).join("")));openDialog("document-dialog");requestAnimationFrame(()=>mark.scrollIntoView({block:"center"}));}else{pre.textContent=documentText;openDialog("document-dialog");pre.scrollTop=0;}}catch(error){toast(error.message);}}
$("source-button").onclick=()=>showSource();$("validation-button").onclick=()=>{if(!graph)return;$("validation-content").textContent=JSON.stringify(graph.validation,null,2);openDialog("validation-dialog");};
$("new-analysis").onclick=()=>openDialog("upload-dialog");$("top-upload").onclick=()=>openDialog("upload-dialog");$("workspace-nav").onclick=()=>window.scrollTo({top:0,behavior:"smooth"});$("show-demo").onclick=loadDemo;
$("settings-button").onclick=()=>openDialog("settings-dialog");$("top-settings").onclick=()=>openDialog("settings-dialog");
$("save-settings").onclick=async()=>{apiKey=$("api-key").value;try{const model=await(await api("/api/settings/model")).json();$("model-url").value=model.base_url||"https://api.openai.com/v1";$("model-name").value=model.model||"gpt-4.1";$("model-style").value=model.api_style;$("model-key").value="";$("model-key").placeholder=model.key_configured?"已配置 · 留空保留现有密钥":"首次使用请输入 API Key";toast("已连接服务");const status=await(await api("/api/services")).json();$("service-status").replaceChildren();for(const service of status.services){const chip=element("span",undefined,"service-chip"+(service.status==="online"?"":" offline"));chip.append(element("span",undefined,"status-dot"),document.createTextNode(`${service.name} · ${service.status==="online"?"在线":"未连接"}`));$("service-status").append(chip);}}catch(error){toast(error.message);}};
$("model-form").onsubmit=async event=>{event.preventDefault();$("save-model").disabled=true;$("model-error").textContent="";apiKey=$("api-key").value;try{await api("/api/settings/model",jsonOptions("PUT",{base_url:$("model-url").value,model:$("model-name").value,api_style:$("model-style").value,api_key:$("model-key").value||null}));$("model-key").value="";$("model-key").placeholder="已配置 · 留空保留现有密钥";await refreshConfig();toast("模型配置已保存，下一次分析立即使用");}catch(error){$("model-error").textContent=error.message;}finally{$("save-model").disabled=false;}};
function setFile(file){pendingFile=file;$("upload-filename").textContent=file?.name||"点击选择或拖入文档";$("file-input").required=!file;}
async function checkServicesForFile(file){
  const status=await(await api("/api/services")).json();
  if(!status.services.some(service=>service.name==="n8n"&&service.status==="online"))throw new Error("n8n 服务未启动或尚未就绪。请运行 start-service.ps1，再在“服务配置”中确认 n8n 在线。");
  if(!/\.(md|txt)$/i.test(file.name)&&!status.services.some(service=>service.name==="Docling"&&service.status==="online"))throw new Error("Docling 文档解析服务尚未就绪，请等待服务启动完成后再提交。");
}
$("file-input").onchange=()=>setFile($("file-input").files[0]);$("drop-zone").ondragover=e=>{e.preventDefault();};$("drop-zone").ondrop=e=>{e.preventDefault();setFile(e.dataTransfer.files[0]);};
$("upload-form").onsubmit=async event=>{event.preventDefault();$("upload-error").textContent="";if(!pendingFile)return;if(authRequired&&!apiKey){$("upload-error").textContent="请先在服务配置中填写 APP_API_KEY。";return;}$("start-button").disabled=true;try{await checkServicesForFile(pendingFile);const form=new FormData();form.append("file",pendingFile);const job=await(await api("/api/jobs",{method:"POST",body:form})).json();currentJob=job.job_id;clearTimeout(pollTimer);await api(`/api/jobs/${currentJob}/launch`,jsonOptions("POST",{mode:"n8n"}));$("upload-dialog").close();$("job-progress").hidden=false;$("progress-title").textContent=`正在分析：${job.filename}`;$("progress-detail").textContent="等待工作流调度";$("progress-bar").value=0;toast("任务已创建，正在分析文档");poll(currentJob);}catch(error){$("upload-error").textContent=error.message;}finally{$("start-button").disabled=!modelConfigured;}};
async function poll(jobId){
  if(currentJob!==jobId)return;
  try{
    const job=await(await api(`/api/jobs/${jobId}`)).json();if(currentJob!==jobId)return;
    $("job-progress").hidden=false;$("retry-job").hidden=job.status!=="failed";
    $("progress-title").textContent=`${statuses[job.status]||job.status} · ${job.filename}`;
    $("progress-detail").textContent=job.error||job.message;$("progress-bar").value=job.progress;
    if(["completed","failed"].includes(job.status)){reanalysisRunning=false;$("reanalyze").disabled=false;}
    if(job.status==="completed"){
      const result=await(await api(`/api/jobs/${jobId}/graph`)).json();if(currentJob!==jobId)return;
      setGraph(result);$("job-progress").hidden=true;toast("文档概念关系图已生成");return;
    }
    if(job.status==="failed")return;
    if(["queued","waiting"].includes(job.status)&&Date.now()-Date.parse(job.updated_at)>90000)$("progress-detail").textContent="等待工作流继续，请检查 n8n 执行记录。";
    pollTimer=setTimeout(()=>poll(jobId),1800);
  }catch(error){toast(error.message);$("progress-detail").textContent="状态查询中断，请从分析记录重新打开任务。";}
}
$("retry-job").onclick=async()=>{$("retry-job").disabled=true;try{await api(`/api/jobs/${currentJob}/retry`,jsonOptions("POST",{mode:"n8n"}));clearTimeout(pollTimer);await poll(currentJob);}catch(error){toast(error.message);}finally{$("retry-job").disabled=false;}};
$("reanalyze").onclick=async()=>{
  if(!graph||graph.demo||reanalysisRunning)return;$("reanalyze").disabled=true;
  try{
    await checkServicesForFile({name:"parsed.md"});
    reanalysisRunning=true;
    const job=await(await api(`/api/jobs/${graph.job_id}/reanalyze`,{method:"POST"})).json();
    currentJob=job.job_id;clearTimeout(pollTimer);
    await api(`/api/jobs/${currentJob}/launch`,jsonOptions("POST",{mode:"n8n"}));
    toast("已创建新的分析记录，使用全文重新筛选重要概念");await poll(currentJob);
  }catch(error){reanalysisRunning=false;toast(error.message);}finally{$("reanalyze").disabled=reanalysisRunning;}
};
$("history-button").onclick=async()=>{try{const jobs=await(await api("/api/jobs")).json();$("history-list").replaceChildren();if(!jobs.length)$("history-list").append(element("p","还没有分析记录。新建一份文档分析即可开始。"));for(const job of jobs){const button=element("button",undefined,"history-item");button.append(element("strong",job.filename),element("small",`${statuses[job.status]||job.status}${job.counts?.concepts!==undefined?` · ${job.counts.concepts} 个概念 · ${job.counts.relations} 条关系`:""} · ${new Date(job.created_at).toLocaleString("zh-CN")}`));button.onclick=async()=>{$("history-dialog").close();currentJob=job.job_id;clearTimeout(pollTimer);if(job.status==="uploaded"){try{await api(`/api/jobs/${currentJob}/launch`,jsonOptions("POST",{mode:"n8n"}));}catch(error){toast(error.message);return;}}await poll(currentJob);};$("history-list").append(button);}openDialog("history-dialog");}catch(error){toast(error.message);}};
$("top-history").onclick=()=>$("history-button").click();
$("export-button").onclick=()=>{if(!graph)return;$("export-view").value=$("view-mode").value==="all"?"all":"backbone";$("export-notice").textContent=graph.demo?"当前导出的是内置人工示例，并非模型分析结果。":"SVG 默认隐藏关系名称，悬停连线可查看；draw.io 与 Mermaid 保留关系名称。JSON 保留所有关系及证据。";openDialog("export-dialog");};
for(const button of document.querySelectorAll("[data-format]"))button.onclick=async()=>{try{const format=button.dataset.format;const path=graph.demo?`/api/demo/export/${format}`:`/api/jobs/${graph.job_id}/export/${format}`;const response=await api(`${path}?view=${$("export-view").value}`);const url=URL.createObjectURL(await response.blob());const link=element("a");link.href=url;link.download=`${graph.demo?"demo":"concept-map"}.${format}`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);toast("图谱文件已导出");}catch(error){toast(error.message);}};
async function refreshConfig(){const config=await(await api("/api/config")).json();modelConfigured=config.model_configured;authRequired=config.auth_required;$("config-notice").textContent=modelConfigured?`模型接口已配置 · 单文件上限 ${config.max_upload_mb} MB · 完整文本上限 ${config.max_document_chars.toLocaleString()} 字符`:`尚未配置模型接口。请打开「服务配置」填写 GPT API，即可分析真实文档。当前可查看内置示例。`;$("start-button").disabled=!modelConfigured;}
async function initialize(){try{await refreshConfig();}catch(error){toast(error.message);}await loadDemo();}
initialize();

