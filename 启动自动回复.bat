@echo off
chcp 65001 >nul
call "D:\softwares\Anaconda3\condabin\conda.bat" activate localgpt
cd /d D:\Gitspace\wechatauto-replica
python -m wechatauto.auto_reply_gui
if errorlevel 1 pause
