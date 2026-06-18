Dim WshShell, Shortcut, desktop
Set WshShell = CreateObject("WScript.Shell")
desktop = WshShell.SpecialFolders("Desktop")

Set Shortcut = WshShell.CreateShortcut(desktop & "\Crypto Strategy Clock.lnk")
Shortcut.TargetPath       = "C:\Users\Zachg\Claude\Projects\Investment Strategy Clock\Open Dashboard.bat"
Shortcut.WorkingDirectory = "C:\Users\Zachg\Claude\Projects\Investment Strategy Clock"
Shortcut.IconLocation     = "C:\Windows\System32\imageres.dll,15"
Shortcut.Description      = "Open Crypto Strategy Clock Dashboard"
Shortcut.WindowStyle      = 7
Shortcut.Save()

MsgBox "Done! 'Crypto Strategy Clock' icon is now on your Desktop." & Chr(13) & Chr(13) & "Double-click it anytime to open your dashboard.", 64, "Crypto Strategy Clock"
