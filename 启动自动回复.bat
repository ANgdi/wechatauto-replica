@echo off
chcp 65001 >nul
cd /d D:\Gitspace\wechatauto-replica
rem AI config: see .env in this folder
"D:\softwares\Anaconda3\envs\localgpt\python.exe" -m wechatauto.auto_reply_gui
if errorlevel 1 pause