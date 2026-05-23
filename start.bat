@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 菜鸟物流查询服务
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start_helper.ps1"
