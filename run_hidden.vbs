' SBS2Flat - launch hidden (no command window)
' Double-click this, or use it as your startup shortcut, to run SBS2Flat in the
' background with no visible console window.
' Edit the two paths below if your setup differs.

Dim shell, pyExe, script, folder
folder = "C:\SBS2Flat\app"          ' <-- folder containing sbs2flat.py
pyExe  = "pythonw.exe"               ' pythonw = Python with no console window

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = folder
' 0 = hidden window, False = don't wait
shell.Run """" & pyExe & """ """ & folder & "\sbs2flat.py""", 0, False
