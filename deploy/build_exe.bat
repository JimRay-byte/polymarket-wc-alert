@echo off
REM ===========================================================================
REM Windows 客户端 PyInstaller 打包脚本
REM 用法：双击运行或在 cmd 中执行 build_exe.bat
REM 前置：pip install pyinstaller websockets plyer
REM ===========================================================================
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [!] 未找到 PyInstaller，正在安装...
    python -m pip install pyinstaller
)

python -c "import websockets, winotify, plyer, pystray, PIL, win32com, certifi" >nul 2>nul
if errorlevel 1 (
    echo [!] 正在安装客户端依赖...
    python -m pip install "websockets>=12.0" "winotify>=1.1" "plyer>=2.1" "pystray>=0.19" "Pillow>=10.0" "win10toast>=0.9" "pywin32>=306" "certifi>=2024.0"
)

echo [*] 开始打包 Windows 客户端...
python -m PyInstaller --noconfirm --onefile --windowed ^
    --name "PolymarketAlert" ^
    --collect-all winotify ^
    --collect-all plyer ^
    --collect-all pystray ^
    --collect-data certifi ^
    --hidden-import win32com.shell ^
    --hidden-import win32com.propsys ^
    client\windows_client.py

if exist dist\PolymarketAlert.exe (
    echo.
    echo [✓] 打包成功：dist\PolymarketAlert.exe
    echo [*] 将 config.json 放到 exe 同目录后运行。
) else (
    echo [✗] 打包失败，请检查上面的错误信息。
)
pause
