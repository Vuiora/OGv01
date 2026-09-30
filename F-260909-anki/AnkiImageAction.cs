using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using HtmlAgilityPack;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Quicker.Public;

// All functionality is embedded in a single Quicker action. No companion action is required.
public static class Script
{
    public const string Title = "Anki 图片编辑并回链";
    private const string Endpoint = "http://127.0.0.1:8765/";
    private static int running;

    public sealed class ImageRef
    {
        public string Field;
        public string Source;
        public string Name;
        public override string ToString() { return Field + "  |  " + Name; }
    }

    public static string Exec(IStepContext context)
    {
        if (Interlocked.Exchange(ref running, 1) != 0) return "动作已在运行";
        long sourceId = 0;
        string profile = null;
        string mediaDir = null;
        bool browserMoved = false;
        string backupDir = null;
        try
        {
            // Read-only diagnostics are also callable from the exported action.
            if (Convert.ToString(context.GetVarValue("_sys_actionParam")) == "diagnose")
            {
                return "C# 编译成功；AnkiConnect 版本 " + Rpc("version")
                    + "；当前配置 " + Rpc("getActiveProfile");
            }
            profile = Convert.ToString(Rpc("getActiveProfile"));
            mediaDir = Convert.ToString(Rpc("getMediaDirPath"));
            var selected = (JArray)Rpc("guiSelectedNotes");
            if (selected.Count != 1) throw new Exception("请打开 Anki 浏览窗口，只选中一条笔记，再双击左 Ctrl。");
            sourceId = (long)selected[0];

            // AnkiConnect does not synchronize open editors. Unload before reading/writing.
            Rpc("guiBrowse", new { query = "nid:0" });
            browserMoved = true;
            Thread.Sleep(400);
            JObject source = Note(sourceId);
            List<ImageRef> images = Images(source);
            if (images.Count == 0) throw new Exception("所选笔记的字段 HTML 没有可搜索的 img src。内嵌 data: 图片不支持 Everything 检索。");
            ImageRef img = images.Count == 1 ? images[0] : Pick("选择要编辑的图片", images, x => x.ToString());

            // The Everything query uses a decoded, literal file name, not an HTTP address.
            List<string> paths = AnkiImageEverything.Search(img.Name);
            if (paths.Count == 0) throw new Exception("Everything 没有找到：" + img.Name + "\n请确认图片文件已被 Everything 索引。");
            paths = paths.OrderBy(p => IsMediaPath(p, mediaDir) ? 1 : 0).ThenBy(p => p).ToList();
            string imagePath = paths.Count == 1 ? paths[0] : Pick("选择画图要编辑的文件（优先列出媒体库以外的原图）", paths, p => p);
            if (!Regex.IsMatch(Path.GetExtension(imagePath), @"^\.(png|jpe?g|bmp|gif|tiff?|webp)$", RegexOptions.IgnoreCase))
                throw new Exception("此格式不在画图支持的图片格式列表中：" + imagePath);

            long targetId = FindTarget(sourceId, img);
            if (targetId == sourceId) throw new Exception("原图目标不能是当前笔记自身。");
            EnsureCollection(profile, mediaDir);

            backupDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Quicker", "AnkiImageBackups", DateTime.Now.ToString("yyyyMMdd_HHmmss") + "_" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(backupDir);
            string imageBackup = Path.Combine(backupDir, Path.GetFileName(imagePath));
            File.Copy(imagePath, imageBackup, false);
            File.WriteAllText(Path.Combine(backupDir, "session.json"), JsonConvert.SerializeObject(new {
                sourceId, targetId, profile, mediaDir, imagePath, imageBackup, field = img.Field,
                imageSource = img.Source, sourceBeforeEdit = source
            }, Formatting.Indented), Encoding.UTF8);

            string beforeHash = Hash(imagePath);
            Process.Start(new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "mspaint.exe"),
                Arguments = "\"" + imagePath + "\"", UseShellExecute = true
            });
            if (!WaitForSave(imagePath, beforeHash)) return "已取消回链。图片备份：" + backupDir;

            EnsureCollection(profile, mediaDir);
            // The user may have browsed to another note while drawing. Flush again, then re-read.
            Rpc("guiBrowse", new { query = "nid:0" });
            Thread.Sleep(400);
            JObject latest = Note(sourceId);
            Note(targetId); // Confirm the target still exists.
            var fields = (JObject)latest["fields"];
            if (fields[img.Field] == null) throw new Exception("图片编辑期间笔记字段发生变化，未写入链接。");
            string oldHtml = (string)fields[img.Field]["value"];
            if (!Images(latest).Any(x => x.Field == img.Field && x.Source == img.Source))
                throw new Exception("图片编辑期间该字段的图片引用发生变化，未写入链接。");
            string newHtml = AppendLink(oldHtml, targetId);
            if (newHtml != oldHtml)
            {
                // AnkiConnect updates are not undoable; preserve the exact latest field first.
                File.WriteAllText(Path.Combine(backupDir, "note-before-link.json"), JsonConvert.SerializeObject(new {
                    noteId = sourceId, field = img.Field, html = oldHtml, profile, mediaDir
                }, Formatting.Indented), Encoding.UTF8);
                Rpc("updateNoteFields", new { note = new {
                    id = sourceId, fields = new Dictionary<string, string> { { img.Field, newHtml } }
                }});
                JObject verified = Note(sourceId);
                if ((string)verified["fields"][img.Field]["value"] != newHtml)
                    throw new Exception("写入后的字段验证未通过，请检查笔记。备份：" + backupDir);
            }
            Info("图片已保存，并已连接到原图笔记。\n\n当前笔记：" + sourceId + "\n原图笔记：" + targetId
                + "\n\n图片和字段备份：\n" + backupDir);
            return "完成：" + sourceId + " → " + targetId;
        }
        catch (OperationCanceledException) { return "已取消"; }
        catch (Exception ex)
        {
            Info(ex.Message + (backupDir == null ? "" : "\n\n备份位置：" + backupDir), MessageBoxIcon.Warning);
            throw;
        }
        finally
        {
            if (browserMoved && sourceId != 0)
            {
                try { EnsureCollection(profile, mediaDir); Rpc("guiBrowse", new { query = "nid:" + sourceId }); }
                catch { /* Do not switch a different profile or hide the original error. */ }
            }
            Interlocked.Exchange(ref running, 0);
        }
    }

    public static JToken Rpc(string action, object parameters = null)
    {
        byte[] body = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(new { action, version = 6, @params = parameters ?? new { } }));
        var req = (HttpWebRequest)WebRequest.Create(Endpoint);
        req.Proxy = null;
        req.Method = "POST";
        req.ContentType = "application/json; charset=utf-8";
        req.Timeout = 15000;
        req.ReadWriteTimeout = 15000;
        req.ContentLength = body.Length;
        try
        {
            using (var output = req.GetRequestStream()) output.Write(body, 0, body.Length);
            using (var response = req.GetResponse())
            using (var inputStream = response.GetResponseStream())
            using (var reader = new StreamReader(inputStream, Encoding.UTF8))
            {
                JObject answer = JObject.Parse(reader.ReadToEnd());
                if (answer["error"] != null && answer["error"].Type != JTokenType.Null)
                    throw new Exception("AnkiConnect / " + action + ": " + (string)answer["error"]);
                return answer["result"];
            }
        }
        catch (WebException ex)
        {
            throw new Exception("无法连接 AnkiConnect（127.0.0.1:8765）。请启动 Anki，并启用已安装的 AnkiConnect。\n" + ex.Message, ex);
        }
    }

    private static JObject Note(long id)
    {
        JArray notes = (JArray)Rpc("notesInfo", new { notes = new[] { id } });
        if (notes.Count != 1 || notes[0]["fields"] == null) throw new Exception("笔记不存在或已经删除：" + id);
        return (JObject)notes[0];
    }

    private static void EnsureCollection(string profile, string mediaDir)
    {
        if (!String.Equals(profile, Convert.ToString(Rpc("getActiveProfile")), StringComparison.Ordinal)
            || !String.Equals(mediaDir, Convert.ToString(Rpc("getMediaDirPath")), StringComparison.OrdinalIgnoreCase))
            throw new Exception("动作期间切换了 Anki 用户/集合，已停止写入。请回到原来的用户后重新运行。");
    }

    public static string FileNameFromSource(string source)
    {
        if (String.IsNullOrWhiteSpace(source)) return null;
        source = HtmlEntity.DeEntitize(source).Trim();
        if (Regex.IsMatch(source, @"^(data|javascript):", RegexOptions.IgnoreCase)) return null;
        string path = source;
        Uri uri;
        if (Uri.TryCreate(source, UriKind.Absolute, out uri))
        {
            if (uri.IsFile) path = uri.LocalPath;
            else if (uri.Scheme == "http" || uri.Scheme == "https") path = uri.GetComponents(UriComponents.Path, UriFormat.UriEscaped);
            else return null;
        }
        else { int q = path.IndexOfAny(new[] { '?', '#' }); if (q >= 0) path = path.Substring(0, q); }
        // Split before decoding: %2F and %5C cannot turn a filename into a path.
        path = path.Replace('\\', '/');
        string name = path.Substring(path.LastIndexOf('/') + 1);
        try { name = Uri.UnescapeDataString(name); } catch (UriFormatException) { return null; }
        if (String.IsNullOrWhiteSpace(name) || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) return null;
        return name;
    }

    public static List<ImageRef> Images(JObject note)
    {
        var result = new List<ImageRef>();
        foreach (var field in ((JObject)note["fields"]).Properties().OrderBy(p => (int?)p.Value["order"] ?? 0))
        {
            var doc = new HtmlAgilityPack.HtmlDocument();
            doc.LoadHtml((string)field.Value["value"] ?? "");
            foreach (var node in doc.DocumentNode.Descendants("img"))
            {
                string src = HtmlEntity.DeEntitize(node.GetAttributeValue("src", "")).Trim();
                string name = FileNameFromSource(src);
                if (name == null || result.Any(x => x.Field == field.Name && x.Source == src)) continue;
                result.Add(new ImageRef { Field = field.Name, Source = src, Name = name });
            }
        }
        return result;
    }

    public static string AppendLink(string html, long targetId)
    {
        if (!Regex.IsMatch(targetId.ToString(), @"^\d{13}$")) throw new Exception("Anki Note Linker 的目标必须是恰好 13 位的笔记 ID。");
        if (Regex.IsMatch(HtmlEntity.DeEntitize(html), @"\[((?:[^\[]|\\\[)*?)\|nid" + targetId + @"\]")) return html;
        return html + "<div>[原图|nid" + targetId + "]</div>";
    }

    private static long FindTarget(long sourceId, ImageRef image)
    {
        // Escape Anki search syntax, then verify exact img filename in actual field HTML.
        string quoted = "\"" + image.Name.Replace("\\", "\\\\").Replace("\"", "\\\"")
            .Replace("*", "\\*").Replace("_", "\\_") + "\" -nid:" + sourceId;
        var ids = ((JArray)Rpc("findNotes", new { query = quoted })).Select(t => (long)t).ToArray();
        var matches = new List<JObject>();
        for (int i = 0; i < ids.Length; i += 100)
        {
            var batch = (JArray)Rpc("notesInfo", new { notes = ids.Skip(i).Take(100).ToArray() });
            foreach (JObject n in batch)
                if (n["fields"] != null && Images(n).Any(x => String.Equals(x.Name, image.Name, StringComparison.OrdinalIgnoreCase))) matches.Add(n);
            if (matches.Count >= 200) break;
        }
        if (matches.Count > 0)
        {
            JObject n = Pick("选择原图所在笔记（已排除当前笔记）", matches, x =>
            {
                var doc = new HtmlAgilityPack.HtmlDocument();
                doc.LoadHtml(String.Join(" ", ((JObject)x["fields"]).Properties().Select(p => (string)p.Value["value"])));
                string text = Regex.Replace(HtmlEntity.DeEntitize(doc.DocumentNode.InnerText), @"\s+", " ").Trim();
                if (text.Length > 110) text = text.Substring(0, 110) + "…";
                return x["noteId"] + " | " + x["modelName"] + " | " + text;
            });
            return (long)n["noteId"];
        }
        string value = Prompt("没有找到引用同名图片的其他笔记。\n请输入原图所在笔记的 13 位 ID，或完整 Note Linker 链接。\n原图必须已有一条 Anki 笔记。");
        var match = Regex.Match(value.Trim(), @"^(?:nid)?(\d{13})$");
        if (!match.Success) match = Regex.Match(value, @"\|nid(\d{13})\]");
        if (!match.Success) throw new Exception("未识别到 13 位笔记 ID。");
        long target = Int64.Parse(match.Groups[1].Value);
        if (target == sourceId) throw new Exception("不能将原图链接指向当前笔记自身。");
        Note(target);
        return target;
    }

    public static string Hash(string path)
    {
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
        using (var sha = SHA256.Create()) return Convert.ToBase64String(sha.ComputeHash(stream));
    }

    private static bool IsMediaPath(string path, string mediaDir)
    {
        return Path.GetFullPath(path).StartsWith(Path.GetFullPath(mediaDir).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
    }

    private static Form Dialog(string heading, int height)
    {
        var form = new Form { Text = Title, Width = 840, Height = height, StartPosition = FormStartPosition.CenterScreen,
            FormBorderStyle = FormBorderStyle.Sizable, MinimizeBox = false, MaximizeBox = false, TopMost = true,
            Font = new Font("Microsoft YaHei UI", 10), AutoScaleMode = AutoScaleMode.Dpi };
        var label = new Label { Text = heading, Dock = DockStyle.Top, Height = 65, Padding = new Padding(14, 12, 14, 0) };
        form.Controls.Add(label);
        return form;
    }

    private static T Pick<T>(string heading, IList<T> choices, Func<T, string> label)
    {
        using (Form form = Dialog(heading, 430))
        using (var list = new ListBox { Dock = DockStyle.Fill, HorizontalScrollbar = true, IntegralHeight = false })
        using (var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 52, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(8) })
        {
            foreach (var item in choices) list.Items.Add(label(item));
            if (list.Items.Count > 0) list.SelectedIndex = 0;
            var ok = new Button { Text = "确定", DialogResult = DialogResult.OK, Width = 95 };
            var cancel = new Button { Text = "取消", DialogResult = DialogResult.Cancel, Width = 95 };
            buttons.Controls.Add(ok); buttons.Controls.Add(cancel);
            form.Controls.Add(list); list.BringToFront(); form.Controls.Add(buttons); buttons.BringToFront();
            form.AcceptButton = ok; form.CancelButton = cancel;
            list.DoubleClick += delegate { if (list.SelectedIndex >= 0) { form.DialogResult = DialogResult.OK; form.Close(); } };
            if (form.ShowDialog() != DialogResult.OK || list.SelectedIndex < 0) throw new OperationCanceledException();
            return choices[list.SelectedIndex];
        }
    }

    private static string Prompt(string heading)
    {
        using (Form form = Dialog(heading, 260))
        {
            form.Controls[0].Height = 110;
            var box = new TextBox { Left = 16, Top = 120, Width = 790, Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            var ok = new Button { Text = "确定", Left = 590, Top = 165, Width = 95, DialogResult = DialogResult.OK };
            var cancel = new Button { Text = "取消", Left = 705, Top = 165, Width = 95, DialogResult = DialogResult.Cancel };
            form.Controls.Add(box); form.Controls.Add(ok); form.Controls.Add(cancel); form.AcceptButton = ok; form.CancelButton = cancel;
            form.Shown += delegate { box.Focus(); };
            if (form.ShowDialog() != DialogResult.OK) throw new OperationCanceledException();
            return box.Text;
        }
    }

    private static bool WaitForSave(string path, string initialHash)
    {
        using (Form form = Dialog("在画图中编辑并按 Ctrl+S 保存。检测到内容变化后，会自动回链。\n若另存为其他文件，本动作不会替换卡片图片；请保存回下面的原路径。", 240))
        using (var timer = new System.Windows.Forms.Timer { Interval = 800 })
        {
            var pathLabel = new Label { Text = path, Dock = DockStyle.Fill, Padding = new Padding(14), AutoEllipsis = true };
            var cancel = new Button { Text = "取消等待 / 不回链", Dock = DockStyle.Bottom, Height = 38, DialogResult = DialogResult.Cancel };
            form.Controls.Add(pathLabel); pathLabel.BringToFront(); form.Controls.Add(cancel); cancel.BringToFront();
            form.CancelButton = cancel;
            form.TopMost = false;
            form.StartPosition = FormStartPosition.Manual;
            var area = Screen.PrimaryScreen.WorkingArea;
            form.Location = new Point(area.Right - form.Width - 16, area.Bottom - form.Height - 16);
            string pending = null;
            DateTime stableSince = DateTime.UtcNow;
            timer.Tick += delegate
            {
                try
                {
                    string now = Hash(path);
                    if (now == initialHash) { pending = null; return; }
                    if (now != pending) { pending = now; stableSince = DateTime.UtcNow; return; }
                    if ((DateTime.UtcNow - stableSince).TotalSeconds >= 1.5)
                    {
                        // A complete decodable image is required; timestamps alone are not a save.
                        using (var stream = File.OpenRead(path)) using (var bitmap = Image.FromStream(stream)) { int width = bitmap.Width; }
                        timer.Stop(); form.DialogResult = DialogResult.OK; form.Close();
                    }
                }
                catch (IOException) { pending = null; }
                catch (UnauthorizedAccessException) { pending = null; }
                catch (ArgumentException) { pending = null; }
                catch (OutOfMemoryException) { pending = null; } // GDI+ reports invalid images this way.
            };
            form.Shown += delegate { timer.Start(); };
            return form.ShowDialog() == DialogResult.OK;
        }
    }

    private static void Info(string text, MessageBoxIcon icon = MessageBoxIcon.Information)
    {
        MessageBox.Show(text, Title, MessageBoxButtons.OK, icon);
    }
}
