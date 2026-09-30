# Anki 图片源 → Everything → 画图 → Anki Note Linker

这个方案按**一个组合动作**搭建。它在 Anki 浏览窗口中从当前正在编辑的字段提取第一个 `<img src="...">` 的值，以该值交给 Everything 搜索；选中图片后用画图打开。你保存并**关闭画图**后，动作把 `[原图|nid……]` 写回原字段，Anki Note Linker 会将它识别为指向原图笔记的链接。

## 先说明一个必要前提

Anki Note Linker 链接的是 **Anki 笔记**，不是 Windows 图片文件。它的格式固定为：

```text
[显示标题|nid13位笔记ID]
```

因此“原图”必须已经有一张对应的 Anki 笔记。运行动作前，请把这张原图笔记的 ID 填入变量 `原图笔记ID`（只填数字，或填 `nid1234567890123` 均可）。

如果你实际想要的是“在卡片中点击后直接打开图片文件”，应使用普通 HTML `<a href="file:///...">`，而不是 Anki Note Linker；这需要另做一个版本。

## 前置条件

- Anki Desktop 已安装 `Anki Note Linker`（插件代码 `1077002392`）。
- Everything 正在运行，版本至少为 1.4.1.969。Quicker 的 Everything 搜索模块通过其本机接口查询。
- 画图可由 `C:\Windows\System32\mspaint.exe` 启动。
- 开始运行时：Anki 的**浏览**窗口在前台，已选中目标笔记，光标已经放在含图片的字段内。
- `img` 的 `src` 应是 Everything 能检索到的文件名、相对路径或路径片段。若它是 `https://...` 网络 URL，Everything 无法以 URL 找到本地原图，需改为按文件名搜索的版本。

## 新建变量

在组合动作右侧变量区创建：

| 变量 | 类型 | 初始值 / 来源 |
| --- | --- | --- |
| `原图笔记ID` | 文本 | 运行时输入；例如 `1700000000000` |
| `图片源` | 文本 | 第 1 步自动化脚本的“结果” |
| `搜索结果` | 文件路径列表 | 第 2 步 Everything 搜索的“路径列表” |
| `图片路径` | 文件路径 | 第 3 步选择结果 |

## 动作步骤

1. **输入文本**：标题“原图笔记 ID”，绑定到 `原图笔记ID`。可取消时停止动作。

2. **自动化脚本**：粘贴 [`01_提取当前Anki字段图片src.js`](01_提取当前Anki字段图片src.js)。将它的“结果”输出绑定至 `图片源`。

   这一步会暂时切换当前 Anki 字段到 HTML 编辑器（`Ctrl+Shift+X`）、复制 HTML、再切换回来。脚本只取第一个 `img` 标签的 `src` 属性。

3. **使用 Everything 搜索文件**：

   - 搜索内容：`{图片源}`
   - 扩展名：`png;jpg;jpeg;webp;bmp;gif;tif;tiff`
   - 最大结果数量：`50`
   - 结果的“路径列表”绑定至 `搜索结果`

4. **如果/否则**：当 `{搜索结果.Count} = 0` 时显示“Everything 未找到：{图片源}”，并停止动作。

5. **选择列表**：数据源为 `搜索结果`，标题“选择要编辑的原图”，单选；把选择结果绑定到 `图片路径`。

   不建议不经选择直接取第一项：同名图片在不同文件夹时很容易编辑错文件。

6. **运行程序**：

   - 程序：`C:\Windows\System32\mspaint.exe`
   - 参数：`"{图片路径}"`
   - 勾选“等待程序结束”。

   此时编辑图片、按 `Ctrl+S` 保存，然后关闭画图。关闭画图才会继续下一步；这样不会在尚未保存时提前回链。

7. **窗口操作 → 激活窗口**：选择 Anki 浏览窗口。不要重新点别的字段；第 2 步所在字段仍应保留编辑焦点。

8. **自动化脚本**：粘贴 [`02_写入AnkiNoteLinker原图链接.js`](02_写入AnkiNoteLinker原图链接.js)，输入绑定为：

   ```json
   {
     "originalNoteId": "{原图笔记ID}",
     "linkTitle": "原图"
   }
   ```

   结果不是空时表示已写入。它会切到 HTML 编辑模式、在字段末尾追加链接、按 `Ctrl+S` 保存并切回普通编辑模式。

## 首次测试

先复制一张测试卡片。确认以下三件事后再用于正式卡片：

1. Anki HTML 中的图片写法类似 `<img src="文件名.png">`，且 Everything 能用该值找到原文件。
2. 画图关闭后，链接出现在**同一个字段**的最后一行。
3. 点击 `原图` 能打开目标笔记。

## 已知边界

- 一个字段有多张图片时，目前取第一张。需要时可加一个“从匹配项中选择”的版本。
- 动作需要用 `Ctrl+C` 读取 HTML。剪贴板原本若是**纯文本**会恢复；若原本是文件/图片等非文本格式，Quicker 的自动化脚本无法完整恢复该格式。
- Windows 11 画图若已存在后台实例，“等待进程结束”在少数机器上可能不会等待窗口关闭。遇到这种情况，把第 6 步改为“启动程序（不等待）→ 等待窗口出现（标题含图片文件名）→ 等待该窗口关闭”。
- Note Linker 只处理笔记 ID；它不能把本地路径或网络 URL 自动变成链接目标。

## 资料

- [Anki Note Linker 的链接格式与快捷键](https://ankiweb.net/shared/info/1077002392)
- [Quicker：Everything 搜索模块](https://docs.getquicker.net/v2/xaction/modules/everythingsearch/)
- [Quicker：自动化脚本](https://docs.getquicker.net/v2/xaction/modules/automationscript/)
- [Anki HTML 编辑器（Ctrl+Shift+X）](https://forums.ankiweb.net/t/ctrl-shift-x-keyboard-shortcut-to-toggle-html-editor-not-working/68738/3)
