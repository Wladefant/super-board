' Runs the given command line with no console window (0 = hidden) and waits for it,
' so a scheduled task never hands its console to Windows Terminal.
' Usage: wscript.exe run_hidden.vbs <exe> [args...]
' WScript cannot carry a literal quote, so callers send " as %22 and % as %25 (decoded here).
' Installed to %USERPROFILE%\.veyyon\run\run_hidden.vbs by hidden_window_audit.py fix.
Dim sh, i, a, cmd
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  If cmd <> "" Then cmd = cmd & " "
  cmd = cmd & QuoteArg(Decode(WScript.Arguments(i)))
Next
WScript.Quit sh.Run(cmd, 0, True)

' Quote one argument for CreateProcess so the child's argv gets it back unchanged:
' empty or spaced args are wrapped in quotes, a quote becomes \", and backslashes
' that precede a quote (or the closing quote) are doubled.
Function QuoteArg(s)
  Dim j, ch, bs, out
  If Len(s) > 0 And InStr(s, " ") = 0 And InStr(s, vbTab) = 0 And InStr(s, """") = 0 Then
    QuoteArg = s
    Exit Function
  End If
  out = """"
  bs = 0
  For j = 1 To Len(s)
    ch = Mid(s, j, 1)
    If ch = "\" Then
      bs = bs + 1
    ElseIf ch = """" Then
      out = out & String(bs * 2 + 1, "\") & """"
      bs = 0
    Else
      out = out & String(bs, "\") & ch
      bs = 0
    End If
  Next
  QuoteArg = out & String(bs * 2, "\") & """"
End Function

' Undo the %22 / %25 escapes in one left-to-right pass. Any other % stays as written.
Function Decode(s)
  Dim k, out
  out = ""
  k = 1
  Do While k <= Len(s)
    If Mid(s, k, 3) = "%22" Then
      out = out & """"
      k = k + 3
    ElseIf Mid(s, k, 3) = "%25" Then
      out = out & "%"
      k = k + 3
    Else
      out = out & Mid(s, k, 1)
      k = k + 1
    End If
  Loop
  Decode = out
End Function
