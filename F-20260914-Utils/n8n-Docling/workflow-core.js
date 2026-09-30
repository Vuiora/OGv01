/* Pure helper functions embedded into the n8n workflow. */
function safeName(value) {
  const name = String(value).replace(/\.[^.]+$/, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').slice(0, 70);
  return name && !/^(con|prn|aux|nul|com\d|lpt\d)$/i.test(name) ? name : 'Document';
}
function protect(text) {
  if (text.includes('⟦BT_KEEP_')) throw new Error('原文包含保留标记，请更换文档中的该标记。');
  const kept = {};
  // Preserve code, math and image markup verbatim through the LLM stages.
  const pattern = /(^```[^\n]*\n[\s\S]*?^```[^\n]*(?:\n|$)|^~~~[^\n]*\n[\s\S]*?^~~~[^\n]*(?:\n|$)|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)|(?<![\\$])\$(?!\$)[^$\n]+\$(?!\$)|!\[[^\]]*\]\([^\n]*?\)|<img\b[^>]*>|`+[^`\n]+`+)/gmi;
  const masked = text.replace(pattern, value => {
    const key = '⟦BT_KEEP_' + String(Object.keys(kept).length).padStart(5, '0') + '⟧';
    kept[key] = value;
    return key;
  });
  return {masked, kept};
}
function tokens(text) { return String(text).match(/⟦BT_KEEP_\d+⟧/g) || []; }
function restore(text, kept) {
  return text.replace(/⟦BT_KEEP_\d+⟧/g, key => {
    if (!(key in kept)) throw new Error('发现未知保留标记：' + key);
    return kept[key];
  });
}
function checkTokens(source, translation) {
  if (JSON.stringify(tokens(source)) !== JSON.stringify(tokens(translation)))
    throw new Error('模型遗漏、改动或调换了代码/公式/图片标记，已停止导入。请重试该阶段。');
}
function splitDocument(text, limit, mode) {
  const lines = (text.match(/[^\n]*(?:\n|$)/g) || []).filter(Boolean);
  const pieces = [];
  for (let i = 0; i < lines.length; i++) {
    let piece = lines[i];
    if (/^\s*\|/.test(piece)) {
      while (i + 1 < lines.length && /^\s*\|/.test(lines[i + 1])) piece += lines[++i];
      if (piece.length > limit) throw new Error('一个表格超过切分长度，请提高每段字符上限，或单独处理该表格。');
    } else if (piece.trim() && !/^#{1,6}\s/.test(piece)) {
      while (i + 1 < lines.length && lines[i + 1].trim() && !/^(?:#{1,6}\s|\s*\|)/.test(lines[i + 1])) piece += lines[++i];
    }
    pieces.push(piece);
  }
  const chunks = []; let current = '';
  const flush = () => {if (current) chunks.push(current); current = '';};
  for (let piece of pieces) {
    if (mode === 'headings' && /^#{1,3}\s/.test(piece) && current.trim()) flush();
    while (piece.length > limit) {
      flush();
      let cut = piece.lastIndexOf(' ', limit);
      if (cut < limit / 2) cut = limit;
      // Never split a protected token or a Unicode surrogate pair.
      for (const m of piece.matchAll(/⟦BT_KEEP_\d+⟧/g)) if (m.index < cut && m.index + m[0].length > cut) cut = m.index;
      if (cut > 0 && /[\uD800-\uDBFF]/.test(piece[cut - 1])) cut--;
      if (!cut) throw new Error('切分边界无效。');
      chunks.push(piece.slice(0, cut)); piece = piece.slice(cut);
    }
    if (current.length + piece.length > limit) flush();
    current += piece;
  }
  flush();
  if (chunks.join('') !== text) throw new Error('切分校验失败，原文内容不一致。');
  return chunks;
}
function completion(response) {
  const choice = response.choices && response.choices[0];
  if (!choice || choice.finish_reason !== 'stop') throw new Error('模型输出未正常结束（可能被截断）。已停止导入。');
  const text = choice.message && choice.message.content;
  if (typeof text !== 'string' || !text.trim()) throw new Error('模型返回了空内容。');
  return text;
}
function parseJson(text) {
  const clean = String(text).trim().replace(/^```(?:json)?\s*\n?/, '').replace(/\n?```\s*$/, '');
  try {return JSON.parse(clean);} catch {throw new Error('模型未返回合法 JSON，请重试该阶段。');}
}
function normalizeReviewResult(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result) || typeof result.translation !== 'string' || !result.translation.trim()) {
    throw new Error('复核没有返回完整译文，已停止导入。请重试复核。');
  }
  // The translation is required; an omitted diagnostic list is not lost text.
  // Keep a visible warning instead of claiming the reviewer found no issues.
  let issues;
  if (result.issues === undefined || result.issues === null) {
    issues = ['复核返回了译文，但未提供问题清单，请人工核对该段。'];
  } else if (Array.isArray(result.issues)) {
    issues = result.issues.map(value => typeof value === 'string' ? value : '结构化核对记录：' + JSON.stringify(value)).filter(Boolean);
  } else if (typeof result.issues === 'string') {
    issues = result.issues.trim() ? [result.issues] : [];
  } else {
    issues = ['复核问题清单格式异常，请人工核对：' + JSON.stringify(result.issues)];
  }
  return {translation:result.translation, issues};
}
function numericWarnings(source, translation) {
  const withoutTokens = s => s.replace(/⟦BT_KEEP_\d+⟧/g, '');
  const nums = s => [...new Set(withoutTokens(s).match(/\d+(?:[.,]\d+)*(?:%|％)?/g) || [])];
  return nums(source).filter(n => !nums(translation).includes(n)).map(n => '请核对数值：' + n);
}
function emphasize(translation, plan) {
  const spans = []; const warnings = [];
  for (const [key, marker] of [['highlight','=='],['bold','**']]) {
    if (plan[key] !== undefined && !Array.isArray(plan[key])) throw new Error('排版结果格式错误。');
    for (const phrase of (plan[key] || []).slice(0, 12)) {
      if (typeof phrase !== 'string' || !phrase.trim() || phrase.length > 100 || /[\n\r`*#=<>⟦⟧]/.test(phrase)) {warnings.push('跳过不合适的强调片段');continue;}
      let at = translation.indexOf(phrase);
      if (at < 0) {warnings.push('跳过非原文片段：' + phrase);continue;}
      const end = at + phrase.length;
      if (spans.some(s => at < s.end && end > s.at)) continue;
      // Avoid emphasis inside links, inline HTML or existing emphasis markers.
      const lineStart = translation.lastIndexOf('\n', at) + 1;
      const prefix = translation.slice(lineStart, at);
      if ((prefix.match(/\*\*/g) || []).length % 2 || (prefix.match(/==/g) || []).length % 2 || prefix.lastIndexOf('](') > prefix.lastIndexOf(')')) continue;
      spans.push({at, end, marker});
    }
  }
  spans.sort((a,b)=>a.at-b.at);
  let output = '', reconstructed = '', cursor = 0;
  for (const s of spans) {
    const unchanged = translation.slice(cursor, s.at), phrase = translation.slice(s.at, s.end);
    output += unchanged + s.marker + phrase + s.marker;
    reconstructed += unchanged + phrase; cursor = s.end;
  }
  output += translation.slice(cursor); reconstructed += translation.slice(cursor);
  if (reconstructed !== translation) throw new Error('排版一致性校验失败。');
  return {text:output, warnings, count:spans.length};
}
if (typeof module !== 'undefined') module.exports = {safeName,protect,tokens,restore,checkTokens,splitDocument,completion,parseJson,normalizeReviewResult,numericWarnings,emphasize};
