Option Explicit
' Start the GUI directly via the Python 3.11 windowed interpreter.
' No cmd.exe, no console window and no terminal focus stealing.
Dim shell, fso, base, launcher, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(fso.BuildPath(base, "_internal"), "start_gui.pyw")
shell.CurrentDirectory = base
If Not fso.FileExists(launcher) Then
    MsgBox "Application files are missing:" & vbCrLf & launcher, vbCritical, "Unified Transcriber"
    WScript.Quit 1
End If
command = "pyw.exe -3.11 -X utf8 " & Chr(34) & launcher & Chr(34)
On Error Resume Next
' 1 = display the GUI normally; pythonw/pyw do not create a console.
shell.Run command, 1, False
If Err.Number <> 0 Then
    MsgBox "Unable to start. Please install Python 3.11 (with Python launcher)." & vbCrLf & Err.Description, vbCritical, "Unified Transcriber"
    WScript.Quit 1
End If
On Error GoTo 0
