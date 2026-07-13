Option Explicit

Dim shell, fso, repoRoot, logsDir, launcherLog, backendScript, dashboardUrl
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoRoot = fso.GetParentFolderName(WScript.ScriptFullName)
logsDir = repoRoot & "\logs"
launcherLog = logsDir & "\dashboard_launcher.log"
backendScript = repoRoot & "\start_backend_hidden.bat"
dashboardUrl = "http://127.0.0.1:8000/"

If Not fso.FolderExists(logsDir) Then
  fso.CreateFolder logsDir
End If

WriteLog "Starting hidden dashboard launcher."
shell.Run Chr(34) & backendScript & Chr(34), 0, False

If WaitForDashboard(dashboardUrl, 30) Then
  WriteLog "Dashboard responded. Opening browser."
Else
  WriteLog "Dashboard did not respond within 30 seconds. Opening browser anyway."
End If

shell.Run dashboardUrl & "clean-slate?source=launcher", 1, False

Sub WriteLog(message)
  Dim stream
  Set stream = fso.OpenTextFile(launcherLog, 8, True)
  stream.WriteLine "[" & Now & "] " & message
  stream.Close
End Sub

Function WaitForDashboard(url, secondsToWait)
  Dim attempt, request
  WaitForDashboard = False

  For attempt = 1 To secondsToWait
    WScript.Sleep 1000
    On Error Resume Next
    Set request = CreateObject("WinHttp.WinHttpRequest.5.1")
    request.Open "GET", url, False
    request.SetTimeouts 1000, 1000, 1000, 1000
    request.Send
    If Err.Number = 0 Then
      If request.Status >= 200 And request.Status < 500 Then
        WaitForDashboard = True
        Exit Function
      End If
    End If
    Err.Clear
    On Error GoTo 0
  Next
End Function
