@echo off
chcp 65001 >nul
rem ===== 可选：启用 AI 时取消下面注释并填值 =====
rem set WECHATAUTO_AI_KEY=sk-xxxx
rem set WECHATAUTO_AI_BASE=https://api.deepseek.com/v1
rem set WECHATAUTO_AI_MODEL=deepseek-chat
call "D:\softwares\Anaconda3\condabin\conda.bat" activate localgpt
cd /d D:\Gitspace\wechatauto-replica
python -m wechatauto.auto_reply_gui
if errorlevel 1 pause
