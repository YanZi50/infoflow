@echo off
cd /d "%~dp0"

set PY=C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

rem 取当前 git 短哈希作为版本号（无 git 时用日期）
set VER=dev
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set VER=%%i
if "%VER%"=="dev" for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set VER=%%i
set ZIPNAME=信息流素材一键拼接-便携版-%VER%.zip

echo [1/3] 用 PyInstaller 打包（onedir + 无控制台 + 应用图标）...
"%PY%" -m PyInstaller --noconfirm --clean --onedir --noconsole ^
  --name "信息流素材一键拼接" ^
  --add-data "web;web" ^
  --add-data "version.txt;." ^
  --add-data "models;models" ^
  --collect-all ctranslate2 ^
  --collect-submodules imageio_ffmpeg ^
  --collect-submodules faster_whisper ^
  --icon app.ico ^
  web_app.py
if errorlevel 1 ( echo 打包失败 & pause & exit /b 1 )

echo [2/3] 复制 ffmpeg.exe 到 exe 旁 bin 目录...
"%PY%" -c "import shutil, os, imageio_ffmpeg; dist=os.path.join('dist', '信息流素材一键拼接'); os.makedirs(os.path.join(dist, 'bin'), exist_ok=True); shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(dist, 'bin', 'ffmpeg.exe'))"
if errorlevel 1 ( echo 复制 ffmpeg 失败 & pause & exit /b 1 )

rem 拷贝 README 使用文档进便携版
if exist "README.md" copy /y "README.md" "dist\信息流素材一键拼接\README使用文档.md" >nul

echo [3/3] 压缩便携版并输出到 D:\Myfolder\doubao\...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\信息流素材一键拼接\*' -DestinationPath 'D:\Myfolder\doubao\%ZIPNAME%' -Force"
if errorlevel 1 ( echo 压缩失败 & pause & exit /b 1 )

echo 完成：
echo   便携版压缩包：D:\Myfolder\doubao\%ZIPNAME%
echo   解压后双击 信息流素材一键拼接.exe 即可使用（自动打开浏览器）。
pause
