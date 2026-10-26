@echo off
chcp 65001 >nul
rem AI 的 Key 写在同目录 .env 里（模板见 .env.example）
call "D:\softwares\Anaconda3\condabin\conda.bat" activate localgpt
cd /d D:\Gitspace\wechatauto-replica
python -m wechatauto.auto_reply_gui
if errorlevel 1 pause
