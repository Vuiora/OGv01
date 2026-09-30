"use strict";
const $ = id => document.getElementById(id);
const labels = {domain:"领域",category:"类别",policy:"策略",implementation:"实现算法",mechanism:"机制",concept:"概念",example:"示例",is_a:"属于",part_of:"组成部分",implements:"实现",handled_by:"由其处理",depends_on:"依赖",contrasts_with:"对比",uses:"使用"};
let mode = "files", selected = [], config = null, activeId = null, generation = 0, timer = null, graphId = null;
function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
async function request(url, options = {}) {
  const headers = new Headers(options.headers);
  if ($("key").value) headers.set("X-API-Key", $("key").value);
  const response = await fetch(url, {...options, headers, cache:"no-store", credentials:"same-origin", signal: AbortSignal.timeout(30000)});
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map(x => `${x.loc.join(" / ")}：${x.msg}`).join("\n");
    } catch { /* Keep the HTTP status for non-JSON errors. */ }
    if (response.status === 401) { $("connection").hidden = false; $("connection").open = true; message = config?.web_auto_auth ? "页面认证已失效，请刷新页面后重试。" : "请在连接设置中填写有效的 CLC_API_KEY。"; }
    throw new Error(message);
  }
  return response;
}
function errorText(error) {
  return error.name === "TimeoutError" ? "请求超时，请查询任务状态后再决定是否重新提交。" : error.message || "连接失败，请检查服务是否启动。";
}
function switchMode(next) {
  mode = next;
  $("file-panel").hidden = next !== "files"; $("text-panel").hidden = next !== "text";
  $("file-tab").setAttribute("aria-selected", next === "files");
  $("text-tab").setAttribute("aria-selected", next === "text");
}
$("file-tab").onclick = () => switchMode("files");
$("text-tab").onclick = () => switchMode("text");
function renderFiles() {
  $("file-list").replaceChildren();
  selected.forEach((file, index) => {
    const li = element("li"); li.append(element("span", `${file.name} · ${(file.size / 1024).toFixed(1)} KB`));
    const remove = element("button", "移除"); remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${file.name}`);
    remove.onclick = () => { selected.splice(index, 1); renderFiles(); };
    li.append(remove); $("file-list").append(li);
  });
}
function addFiles(files) {
  $("submit-error").textContent = "";
  for (const file of files) {
    if (!/\.(md|txt|pdf|docx)$/i.test(file.name)) { $("submit-error").textContent = "仅支持 .md、.txt、.pdf 和 .docx 文件。"; continue; }
    if (!selected.some(f => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified)) selected.push(file);
  }
  renderFiles();
}
$("files").onchange = e => { addFiles(e.target.files); e.target.value = ""; };
for (const event of ["dragenter", "dragover"]) $("drop").addEventListener(event, e => { e.preventDefault(); $("drop").classList.add("dragging"); });
for (const event of ["dragleave", "drop"]) $("drop").addEventListener(event, e => { e.preventDefault(); $("drop").classList.remove("dragging"); });
$("drop").addEventListener("drop", e => addFiles(e.dataTransfer.files));
$("text").oninput = () => { $("count").textContent = `${Array.from($("text").value).length.toLocaleString()} 字符`; };
$("example").onclick = () => {
  if ($("text").value && !confirm("用示例替换当前文字？")) return;
  $("title").value = "Linux 调度策略与 CFS";
  $("text").value = "SCHED_NORMAL 是普通任务的调度策略。CFS 是普通任务公平调度的实现算法。CFS 实现 SCHED_NORMAL 的普通任务调度。策略描述如何对待一类任务，实现算法决定具体如何选择下一个运行的任务。";
  $("text").oninput();
};
function requireKey() {
  if (config?.auth_required && !config.web_auto_auth && !$("key").value) { $("connection").open = true; $("key").focus(); throw new Error("请先填写连接设置中的服务密钥 CLC_API_KEY。"); }
}
$("create").onsubmit = async event => {
  event.preventDefault(); $("submit-error").textContent = "";
  const button = $("submit"); button.disabled = true; button.textContent = "正在提交…";
  try {
    requireKey();
    const title = $("title").value.trim(); if (!title) throw new Error("请填写知识文档标题。");
    if (config && !config.llm_configured) throw new Error("模型尚未配置，请填写服务器 .env 中的模型参数并重启服务。");
    const formats = ["md", ...["docx", "pdf"].filter(id => $(id).checked)];
    let url, options;
    if (mode === "text") {
      const text = $("text").value;
      if (!text.trim()) throw new Error("请粘贴要整理的资料。");
      if (config && Array.from(text).length > config.limits.source_chars) throw new Error("文字超出上限，请拆分后提交。");
      url = "/v1/jobs/text"; options = {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title, text, formats})};
    } else {
      if (!selected.length) throw new Error("请至少选择一个文件。");
      if (selected.some(f => f.size === 0)) throw new Error("包含空文件，请移除后提交。");
      if (config && selected.length > config.limits.files) throw new Error(`最多上传 ${config.limits.files} 个文件。`);
      if (config && selected.reduce((sum, f) => sum + f.size, 0) > config.limits.upload_mb * 1024 * 1024) throw new Error("上传总大小超出限制。");
      if (config && !config.docling_installed && selected.some(f => /\.(pdf|docx)$/i.test(f.name))) throw new Error("服务器尚未安装 Docling，请先使用 Markdown / TXT 或安装解析依赖。");
      const body = new FormData(); body.set("title", title); body.set("formats", formats.join(","));
      selected.forEach(file => body.append("files", file)); url = "/v1/jobs"; options = {method:"POST", body};
    }
    const job = await (await request(url, options)).json();
    $("job-id").value = job.id; await watch(job.id);
  } catch (error) { $("submit-error").textContent = errorText(error); }
  finally { button.disabled = false; button.textContent = "生成知识层级 →"; }
};
$("lookup").onsubmit = event => { event.preventDefault(); watch($("job-id").value.trim()); };
$("refresh").onclick = () => { if (activeId) watch(activeId); };
async function watch(id) {
  $("task-error").textContent = "";
  try { requireKey(); if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) throw new Error("请输入有效的任务 ID。"); }
  catch (error) { $("task-error").textContent = errorText(error); return; }
  clearTimeout(timer); const version = ++generation;
  if (activeId !== id) { graphId = null; $("preview").hidden = true; $("task").hidden = true; $("empty").hidden = false; }
  activeId = id;
  const url = new URL(location.href); url.hash = id; history.replaceState(null, "", url);
  async function poll() {
    try {
      const job = await (await request(`/v1/jobs/${id}`)).json();
      if (version !== generation) return;
      $("task-error").textContent = ""; renderJob(job);
      if (["queued", "running"].includes(job.status)) timer = setTimeout(poll, 2500);
      else if (job.status === "succeeded" && graphId !== id) {
        const graph = await (await request(`/v1/jobs/${id}/graph`)).json();
        if (version !== generation) return;
        renderGraph(graph); graphId = id;
      }
    } catch (error) {
      if (version !== generation) return;
      $("activity").hidden = true;
      $("task-error").textContent = `${errorText(error)} 点击查询或刷新状态可重试。`;
    }
  }
  await poll();
}
function renderJob(job) {
  $("empty").hidden = true; $("task").hidden = false;
  $("status").textContent = {queued:"等待处理",running:"正在构建知识层级",succeeded:"知识层级已生成",failed:"处理失败"}[job.status] || job.status;
  const stages = {queued:"任务已入队。若长时间等待，请确认 Worker 已启动。",starting:"正在启动处理进程",parsing:"正在解析资料",organizing:"正在组织目录与知识关系",exporting:"正在生成文档与下载包",succeeded:"可以浏览下方知识目录，或下载文档。",failed:"请根据错误信息调整后重新提交。"};
  $("stage").textContent = stages[job.stage] || job.stage.replace(/^extracting (\d+)\/(\d+)$/, "正在提取知识点 · 分块 $1 / $2");
  $("activity").hidden = !["queued", "running"].includes(job.status);
  $("task-id").textContent = `任务 ID：${job.id}`;
  $("job-error").textContent = job.error || ""; $("downloads").replaceChildren();
  if (job.status === "succeeded") {
    downloadButton("下载全部 ZIP", job.bundle_url, "knowledge.zip");
    for (const [path, name] of [["knowledge.docx", "Word"], ["knowledge.pdf", "PDF"], ["knowledge.md", "Markdown"]]) {
      const file = job.artifacts.find(f => f.path === path);
      if (file) downloadButton(name, file.url, path);
    }
  }
}
function downloadButton(label, url, name) {
  const button = element("button", label); button.type = "button";
  button.onclick = async () => {
    button.disabled = true; $("task-error").textContent = "";
    try {
      const blob = await (await request(url)).blob(); const href = URL.createObjectURL(blob);
      const link = element("a"); link.href = href; link.download = name; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 60000);
    } catch (error) { $("task-error").textContent = errorText(error); }
    finally { button.disabled = false; }
  };
  $("downloads").append(button);
}
function renderGraph(graph) {
  $("preview").hidden = false; $("tree").replaceChildren(); $("node-detail").replaceChildren();
  $("graph-count").textContent = `${graph.nodes.length} 个节点 · ${graph.relations.length} 条关系`;
  const nodes = new Map(graph.nodes.map(n => [n.id, n])), children = new Map(), buttons = new Map();
  graph.nodes.forEach(node => { const parent = node.parent_id ?? null; if (!children.has(parent)) children.set(parent, []); children.get(parent).push(node); });
  function show(node) {
    for (const [id, button] of buttons) button.setAttribute("aria-current", id === node.id);
    const panel = $("node-detail"); panel.replaceChildren(element("h3", node.title), element("span", `${labels[node.kind] || node.kind} · ${node.scope}`, "badge"));
    if (node.synthetic) panel.append(element("p", "此节点是自动组织的阅读目录。", "hint"));
    panel.append(element("p", node.summary, "prose"));
    if (node.explanation) panel.append(element("div", node.explanation, "prose"));
    for (const [title, values] of [["易混点", node.pitfalls], ["示例", node.examples]]) {
      if (values?.length) { panel.append(element("h4", title)); values.forEach(value => panel.append(element("p", value, "prose"))); }
    }
    if (node.evidence?.length) { panel.append(element("h4", "原文依据")); node.evidence.forEach(c => panel.append(element("p", c.chunk_id, "hint"), element("blockquote", c.quote))); }
    const relations = graph.relations.filter(r => r.source === node.id || r.target === node.id);
    if (relations.length) panel.append(element("h4", "相关关系"));
    relations.forEach(r => {
      const line = element("p");
      for (const [index, id] of [r.source, r.target].entries()) {
        if (index) line.append(document.createTextNode(` → ${labels[r.kind] || r.kind} → `));
        const link = element("button", nodes.get(id)?.title || id, "link"); link.onclick = () => {
          const target = buttons.get(id); let parent = target?.parentElement;
          while (parent && parent !== $("tree")) { if (parent.tagName === "DETAILS") parent.open = true; parent = parent.parentElement; }
          if (nodes.has(id)) show(nodes.get(id)); target?.scrollIntoView({block:"nearest"});
        }; line.append(link);
      }
      panel.append(line, element("p", r.explanation, "prose"));
      r.evidence.forEach(c => panel.append(element("p", c.chunk_id, "hint"), element("blockquote", c.quote)));
    });
  }
  function branch(parent, depth = 0) {
    const list = element("ul");
    for (const node of children.get(parent) || []) {
      const item = element("li"), button = element("button", node.title); buttons.set(node.id, button); button.onclick = () => show(node); item.append(button);
      if (children.has(node.id) && depth < 12) { const details = element("details"); details.open = depth < 2; details.append(element("summary", `${children.get(node.id).length} 个下级知识点`), branch(node.id, depth + 1)); item.append(details); }
      list.append(item);
    }
    return list;
  }
  $("tree").append(branch(null)); if (graph.nodes.length) show(graph.nodes[0]);
}
async function initialize() {
  try {
    config = await (await request("/health")).json();
    $("health").textContent = config.llm_configured ? "● 服务已连接" : "● 请先配置模型";
    $("limits").textContent = `最多 ${config.limits.files} 个文件 · 合计 ${config.limits.upload_mb} MB · ${config.docling_installed ? "PDF / Word 解析可用" : "PDF / Word 需要安装 Docling"}`;
    $("connection").hidden = config.web_auto_auth || !config.auth_required;
    $("connection").open = config.auth_required && !config.web_auto_auth;
    const id = location.hash.slice(1); if (id) { $("job-id").value = id; if (!config.auth_required || config.web_auto_auth) await watch(id); }
  } catch { $("health").textContent = "● 服务连接失败"; $("limits").textContent = "暂时无法读取上传限制，请检查服务连接。"; }
}
initialize();
