// Quicker「自动化脚本」步骤。
// 输入示例：
// { "originalNoteId": "1700000000000", "linkTitle": "原图" }
// 运行前：已关闭画图，Anki 浏览窗口处于前台，且仍是最初那个字段的编辑焦点。

import { input, key, sleep } from "quicker";

const rawId = String(input?.originalNoteId ?? "").trim();
const noteId = rawId.replace(/^nid/i, "");
const linkTitle = String(input?.linkTitle ?? "原图").trim() || "原图";

if (!/^\d{13,}$/.test(noteId)) {
  throw new Error("原图笔记 ID 无效。应为至少 13 位数字，可带 nid 前缀。");
}

// 以 HTML 模式写入原始文本，避免富文本编辑器把方括号改写。
key.hotkey(["ctrl", "shift"], "x");
sleep(200);
key.hotkey(["ctrl"], "end");
key.type(`\n[${linkTitle}|nid${noteId}]`);
sleep(150);

// 让 Anki 立即落盘，再回到所见即所得编辑器。
key.hotkey(["ctrl"], "s");
sleep(250);
key.hotkey(["ctrl", "shift"], "x");

export default {
  linkedNoteId: noteId,
  linkText: `[${linkTitle}|nid${noteId}]`
};
