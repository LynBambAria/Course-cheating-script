@echo off
chcp 65001 >nul
echo ========================================
echo    刷课脚本 - 自动监控
echo ========================================
echo.
echo 功能: 自动检测「当前任务已达到完成条件」弹窗并点击「下一个」
echo 按 Ctrl+C 停止脚本
echo.
python "%~dp0auto_course.py"
echo.
pause