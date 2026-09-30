Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
result = shell.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & folder & "\Start.ps1""", 0, True)
If result <> 0 Then MsgBox "Startup failed or is taking longer than expected. Check work\n8n-docling\logs and run Start.vbs again.", 48, "n8n + Docling"
