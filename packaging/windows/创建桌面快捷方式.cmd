@echo off
chcp 65001 >nul
set "INTERVIEW_APP_DIR=%~dp0"
powershell.exe -NoLogo -NoProfile -Command "$ErrorActionPreference='Stop'; $folder=$env:INTERVIEW_APP_DIR; $exe=Join-Path $folder '面试伴航.exe'; if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw '请先全部解压程序，再创建快捷方式。' }; $shell=New-Object -ComObject WScript.Shell; $link=$shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '面试伴航（云端版）.lnk')); $link.TargetPath=$exe; $link.WorkingDirectory=$folder; $link.IconLocation=$exe+',0'; $link.Description='面试伴航 Windows 云端版'; $link.Save(); Write-Host '桌面快捷方式已创建，请保留程序文件夹。'"
if errorlevel 1 echo 创建失败，请直接双击面试伴航.exe 启动。
pause
