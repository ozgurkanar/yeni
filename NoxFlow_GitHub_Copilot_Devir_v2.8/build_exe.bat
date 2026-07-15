@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo =====================================================
echo NoxFlow Windows EXE derleme
 echo =====================================================

where py >nul 2>nul
if errorlevel 1 (
  echo HATA: Python Launcher bulunamadi.
  echo Python 3.11 veya 3.12 kurup tekrar deneyin.
  pause
  exit /b 1
)

if not exist .venv-build\Scripts\python.exe (
  echo [1/5] Derleme ortami olusturuluyor...
  py -3.12 -m venv .venv-build 2>nul || py -3.11 -m venv .venv-build
  if errorlevel 1 goto :error
)

call .venv-build\Scripts\activate.bat

echo [2/5] Derleme araclari kuruluyor...
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

echo [3/5] Eski ciktilar temizleniyor...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/5] NoxFlow.exe uretiliyor...
python -m PyInstaller --noconfirm --clean NoxFlow.spec
if errorlevel 1 goto :error

echo [5/5] Dagitim paketi hazirlaniyor...
copy /y README_KULLANICI.md dist\NoxFlow\README_KULLANICI.md >nul
copy /y SURUM_NOTLARI.md dist\NoxFlow\SURUM_NOTLARI.md >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\NoxFlow\*' -DestinationPath 'dist\NoxFlow_Windows_x64.zip' -Force"

echo.
echo BASARILI:
echo   dist\NoxFlow\NoxFlow.exe
echo   dist\NoxFlow_Windows_x64.zip
pause
exit /b 0

:error
echo.
echo DERLEME BASARISIZ. Yukaridaki hatayi GitHub Copilot'a iletin.
pause
exit /b 1
