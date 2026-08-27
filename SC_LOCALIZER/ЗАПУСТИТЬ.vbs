' Запуск SC Localizer без единого чёрного окна.
' cmd/bat всегда показывает консоль хотя бы на миг; WScript.Shell с флагом 0
' не показывает её вообще, а pythonw.exe работает без консоли по своей природе.

Option Explicit
Dim shell, fso, here, pythonw, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

' Ищем pythonw.exe рядом с python.exe, который лежит в PATH.
pythonw = FindPythonw(shell, fso)

If pythonw = "" Then
    MsgBox "Не найден Python." & vbCrLf & vbCrLf & _
           "Установи его с сайта python.org и отметь галочку ""Add to PATH"".", _
           vbCritical, "SC Localizer"
    WScript.Quit 1
End If

' Библиотеки ставим один раз, тихо и без окна.
If Not HasFlask(shell, pythonw) Then
    shell.Run """" & pythonw & """ -m pip install -r requirements.txt --quiet", 0, True
End If

' 0 = окно скрыто, False = не ждём завершения. Браузер откроет сам app.py.
shell.Run """" & pythonw & """ app.py", 0, False

' ---------------------------------------------------------------

Function FindPythonw(sh, f)
    Dim exec, line, path
    FindPythonw = ""
    On Error Resume Next
    ' where.exe покажет все python.exe из PATH
    Set exec = sh.Exec("where python.exe")
    If Err.Number <> 0 Then Exit Function
    Do While Not exec.StdOut.AtEndOfStream
        line = Trim(exec.StdOut.ReadLine())
        If line <> "" Then
            path = f.GetParentFolderName(line) & "\pythonw.exe"
            ' Заглушка из Microsoft Store весит около нуля и Python не запускает.
            If f.FileExists(path) Then
                If f.GetFile(path).Size > 10000 Then
                    FindPythonw = path
                    Exit Function
                End If
            End If
        End If
    Loop
End Function

Function HasFlask(sh, pyw)
    ' Код возврата 0 означает, что импорт прошёл.
    HasFlask = (sh.Run("""" & pyw & """ -c ""import flask, dotenv""", 0, True) = 0)
End Function
