"""Run this once to place a Crypto Strategy Clock icon on your desktop."""
import os, subprocess, sys

desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
target  = r"C:\Users\Zachg\Claude\Projects\Investment Strategy Clock\Open Dashboard.bat"
icon    = r"C:\Windows\System32\imageres.dll,15"

ps = f"""
$s = (New-Object -COM WScript.Shell).CreateShortcut('{desktop}\\Crypto Strategy Clock.lnk')
$s.TargetPath      = '{target}'
$s.WorkingDirectory= 'C:\\Users\\Zachg\\Claude\\Projects\\Investment Strategy Clock'
$s.IconLocation    = '{icon}'
$s.Description     = 'Open Crypto Strategy Clock Dashboard'
$s.WindowStyle     = 7
$s.Save()
Write-Host 'SUCCESS: Desktop shortcut created.'
"""

result = subprocess.run(
    ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps],
    capture_output=True, text=True
)
if "SUCCESS" in result.stdout:
    print("\n✅  Icon added to your Desktop!\n")
else:
    print("stdout:", result.stdout)
    print("stderr:", result.stderr)
