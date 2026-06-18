@echo off
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%USERPROFILE%\Desktop\Investment Strategy Clock.lnk');$s.TargetPath='%~dp0';$s.Save()"
echo Shortcut created on Desktop!
pause
