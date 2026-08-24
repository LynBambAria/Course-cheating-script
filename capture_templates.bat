@echo off
chcp 65001 >nul
echo ========================================
echo    刷课脚本 - 截图工具
echo ========================================
echo.
echo 请确保弹窗「当前任务已达到完成条件」已显示在屏幕上
echo.
echo 步骤1: 将鼠标移到弹窗的左上角，然后按 Enter
pause >nul
python -c "import pyautogui; print(f'LEFT_TOP:{pyautogui.position()[0]},{pyautogui.position()[1]}')" > "%TEMP%\pos1.txt"
set /p POS1=<"%TEMP%\pos1.txt"
echo 已记录: %POS1%
echo.
echo 步骤2: 将鼠标移到弹窗的右下角，然后按 Enter
pause >nul
python -c "import pyautogui; print(f'RIGHT_BOTTOM:{pyautogui.position()[0]},{pyautogui.position()[1]}')" > "%TEMP%\pos2.txt"
set /p POS2=<"%TEMP%\pos2.txt"
echo 已记录: %POS2%
echo.
echo 正在截取弹窗...
python -c ^
"import pyautogui, shutil, sys; from pathlib import Path; ^
p1='%POS1%'.split(':')[1].split(','); p2='%POS2%'.split(':')[1].split(','); ^
left=int(p1[0]); top=int(p1[1]); right=int(p2[0]); bottom=int(p2[1]); ^
w=abs(right-left); h=abs(bottom-top); ^
left=min(left,right); top=min(top,bottom); ^
td=Path(r'%~dp0templates'); td.mkdir(exist_ok=True); ^
img=pyautogui.screenshot(region=(left,top,w,h)); img.save(str(td/'task_complete.png')); ^
print(f'弹窗截图已保存: ({left},{top},{w},{h})')"
echo.
echo 步骤3: 将鼠标移到「下一个」按钮的左上角，然后按 Enter
pause >nul
python -c "import pyautogui; print(f'LEFT_TOP:{pyautogui.position()[0]},{pyautogui.position()[1]}')" > "%TEMP%\pos3.txt"
set /p POS3=<"%TEMP%\pos3.txt"
echo 已记录: %POS3%
echo.
echo 步骤4: 将鼠标移到「下一个」按钮的右下角，然后按 Enter
pause >nul
python -c "import pyautogui; print(f'RIGHT_BOTTOM:{pyautogui.position()[0]},{pyautogui.position()[1]}')" > "%TEMP%\pos4.txt"
set /p POS4=<"%TEMP%\pos4.txt"
echo 已记录: %POS4%
echo.
echo 正在截取按钮...
python -c ^
"import pyautogui, shutil, sys; from pathlib import Path; ^
p3='%POS3%'.split(':')[1].split(','); p4='%POS4%'.split(':')[1].split(','); ^
left=int(p3[0]); top=int(p3[1]); right=int(p4[0]); bottom=int(p4[1]); ^
w=abs(right-left); h=abs(bottom-top); ^
left=min(left,right); top=min(top,bottom); ^
td=Path(r'%~dp0templates'); td.mkdir(exist_ok=True); ^
img=pyautogui.screenshot(region=(left,top,w,h)); img.save(str(td/'next_button.png')); ^
print(f'按钮截图已保存: ({left},{top},{w},{h})')"
echo.
echo ========================================
echo 截图完成！模板已保存到 templates 目录
echo 现在可以运行主脚本了
echo ========================================
pause