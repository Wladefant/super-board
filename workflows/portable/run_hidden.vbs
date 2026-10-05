' Runs the given command line with no console window (0 = hidden) and waits for it,
' so a scheduled task never hands its console to Windows Terminal.
' Usage: wscript.exe run_hidden.vbs <exe> [args...]
' Installed to %USERPROFILE%\.veyyon\run\run_hidden.vbs by hidden_window_audit.py fix.
Dim sh, i, a, cmd
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  a = WScript.Arguments(i)
  If InStr(a, " ") > 0 Then a = """" & a & """"
  If cmd <> "" Then cmd = cmd & " "
  cmd = cmd & a
Next
WScript.Quit sh.Run(cmd, 0, True)
