Set WshShell = CreateObject("WScript.Shell")
ProjectDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\.."
PythonwPath = ProjectDir & "\.venv\Scripts\pythonw.exe"
Command = """" & PythonwPath & """ -m app.moodle_sso_refresher --daemon --interval 1800"
WshShell.CurrentDirectory = ProjectDir
WshShell.Run Command, 0, False
