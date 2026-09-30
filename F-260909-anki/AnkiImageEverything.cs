// Everything IPC helper for Quicker's C# full-source step (.NET Framework 4.x).
// Append this class to the action's script; no extra managed references are needed.
public static class AnkiImageEverything
{
    private static readonly object SearchLock = new object();

    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    private static extern System.IntPtr LoadLibraryExW(string fileName, System.IntPtr file, uint flags);
    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
    private static extern System.IntPtr GetProcAddress(System.IntPtr module, string name);
    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool FreeLibrary(System.IntPtr module);

    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private delegate void SetText([System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)] string text);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate void SetNumber(uint value);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate void SetFlag(int value);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate uint GetNumber();
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate int Query(int wait);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate int IsFile(uint index);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private delegate uint GetPath(uint index, System.Text.StringBuilder path, uint maxCount);
    [System.Runtime.InteropServices.UnmanagedFunctionPointer(System.Runtime.InteropServices.CallingConvention.StdCall)]
    private delegate void Cleanup();

    private static T Export<T>(System.IntPtr module, string name) where T : class
    {
        System.IntPtr address = GetProcAddress(module, name);
        if (address == System.IntPtr.Zero)
            throw new System.InvalidOperationException("Everything SDK 缺少函数：" + name);
        return (T)(object)System.Runtime.InteropServices.Marshal.GetDelegateForFunctionPointer(address, typeof(T));
    }

    private static string FindSdk()
    {
        string dllName = System.IntPtr.Size == 8 ? "Everything64.dll" : "Everything32.dll";
        string processDirectory = System.IO.Path.GetDirectoryName(System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName);
        string[] directories = {
            processDirectory,
            System.AppDomain.CurrentDomain.BaseDirectory,
            System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.ProgramFiles), "Quicker"),
            System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.ProgramFilesX86), "Quicker")
        };
        foreach (string directory in directories)
        {
            if (string.IsNullOrEmpty(directory)) continue;
            string candidate = System.IO.Path.Combine(directory, dllName);
            if (System.IO.File.Exists(candidate)) return candidate;
        }
        throw new System.IO.FileNotFoundException("未找到 Quicker 自带的 " + dllName + "，请检查 Quicker 安装目录。");
    }

    // Searches the existing Everything index; does not change its visible search box.
    // Each query loads a uniquely named DLL copy, isolating SDK globals from Quicker.
    public static System.Collections.Generic.List<string> Search(string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName) || fileName != System.IO.Path.GetFileName(fileName) ||
            fileName.IndexOfAny(System.IO.Path.GetInvalidFileNameChars()) >= 0)
            throw new System.ArgumentException("Everything 搜索需要图片文件名，而不是完整 URL 或路径。", "fileName");

        lock (SearchLock)
        {
            string privateDll = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
                "QuickerAnkiImageEverything_" + System.Guid.NewGuid().ToString("N") + ".dll");
            System.IntPtr module = System.IntPtr.Zero;
            Cleanup cleanup = null;
            try
            {
                System.IO.File.Copy(FindSdk(), privateDll, false);
                // DLL_LOAD_DIR | DEFAULT_DIRS excludes the current working directory.
                module = LoadLibraryExW(privateDll, System.IntPtr.Zero, 0x00000100 | 0x00001000);
                if (module == System.IntPtr.Zero)
                    throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error(), "无法载入 Everything SDK。");

                cleanup = Export<Cleanup>(module, "Everything_CleanUp");
                Export<SetFlag>(module, "Everything_SetMatchPath")(0);
                Export<SetFlag>(module, "Everything_SetMatchCase")(0);
                Export<SetFlag>(module, "Everything_SetMatchWholeWord")(0);
                Export<SetFlag>(module, "Everything_SetRegex")(1);
                Export<SetNumber>(module, "Everything_SetOffset")(0);
                Export<SetNumber>(module, "Everything_SetMax")(10000);
                Export<SetNumber>(module, "Everything_SetRequestFlags")(3);
                Export<SetText>(module, "Everything_SetSearchW")("^" + System.Text.RegularExpressions.Regex.Escape(fileName) + "$");
                if (Export<Query>(module, "Everything_QueryW")(1) == 0)
                {
                    uint error = Export<GetNumber>(module, "Everything_GetLastError")();
                    throw new System.InvalidOperationException("Everything 查询失败（代码 " + error + "）。请先启动 Everything，确认它与 Quicker 使用相同的运行权限。");
                }
                uint count = Export<GetNumber>(module, "Everything_GetNumResults")();
                if (Export<GetNumber>(module, "Everything_GetTotResults")() > count)
                    throw new System.InvalidOperationException("同名结果超过 10000 个，请使用更明确的图片文件名。");
                IsFile isFile = Export<IsFile>(module, "Everything_IsFileResult");
                GetPath getPath = Export<GetPath>(module, "Everything_GetResultFullPathNameW");
                var result = new System.Collections.Generic.List<string>();
                var seen = new System.Collections.Generic.HashSet<string>(System.StringComparer.OrdinalIgnoreCase);
                for (uint i = 0; i < count; i++)
                {
                    if (isFile(i) == 0) continue;
                    var buffer = new System.Text.StringBuilder(32768);
                    uint length = getPath(i, buffer, (uint)buffer.Capacity);
                    if (length == 0 || length >= buffer.Capacity - 1) continue;
                    string path = buffer.ToString();
                    if (string.Equals(System.IO.Path.GetFileName(path), fileName, System.StringComparison.OrdinalIgnoreCase) &&
                        System.IO.File.Exists(path) && seen.Add(path)) result.Add(path);
                }
                result.Sort(System.StringComparer.OrdinalIgnoreCase);
                return result;
            }
            finally
            {
                try { if (cleanup != null) cleanup(); }
                finally
                {
                    if (module != System.IntPtr.Zero) FreeLibrary(module);
                    try { if (System.IO.File.Exists(privateDll)) System.IO.File.Delete(privateDll); }
                    catch (System.IO.IOException) { }
                    catch (System.UnauthorizedAccessException) { }
                }
            }
        }
    }
}
