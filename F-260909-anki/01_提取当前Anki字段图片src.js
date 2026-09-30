// Quicker「自动化脚本」步骤。
// 运行前：Anki 浏览窗口在前台，光标在含图片的字段内。
// 输出：图片 src 的纯文本。

import { clipboard, key, sleep } from "quicker";

const previousText = clipboard.readText();
let html = "";

try {
  // Anki：切换当前字段到 HTML 编辑模式。
  key.hotkey(["ctrl", "shift"], "x");
  sleep(250);

  key.hotkey(["ctrl"], "a");
  key.hotkey(["ctrl"], "c");
  sleep(250);
  html = clipboard.readText() ?? "";
} finally {
  // 无论匹配是否成功，都切回普通编辑模式并恢复原纯文本剪贴板。
  key.hotkey(["ctrl", "shift"], "x");
  sleep(150);
  if (previousText !== null) {
    clipboard.writeText(previousText, { hideFromHistory: true });
  }
}

// 同时支持 src="..."、src='...' 和不加引号的 src=...。
const match = /<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/i.exec(html);
const source = (match?.[1] ?? match?.[2] ?? match?.[3] ?? "").trim();

if (!source) {
  throw new Error("当前字段没有找到 <img src=\"...\">。请先把光标放在含图片的字段内。");
}

export default source;
