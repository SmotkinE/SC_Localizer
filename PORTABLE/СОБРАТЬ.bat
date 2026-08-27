@echo off
REM Файл сохранён в кодировке cp866, иначе cmd.exe не понимает русский текст.
cd /d "%~dp0"

echo ==========================================
echo    Сборка портабл-версии SC Localizer
echo ==========================================
echo.

REM Запущенный exe держит свои файлы, и PyInstaller падает с "отказано в доступе".
REM Закрываем его перед сборкой, иначе получаем кучу PermissionError.
taskkill /IM SC_Localizer.exe /F >nul 2>&1
if not errorlevel 1 (
    echo Закрыл запущенный SC_Localizer.exe
    REM Даём антивирусу отпустить файлы.
    timeout /t 3 >nul
)

echo ВАЖНО: закрой вкладку браузера с программой - открытая страница
echo тоже может держать файлы и мешать сборке.
echo.

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Ставлю PyInstaller...
    python -m pip install pyinstaller --quiet
)

echo Ставлю зависимости программы...
python -m pip install -r ..\SC_LOCALIZERequirements.txt --quiet

echo.
echo Собираю. Это займёт минуту-полторы...
echo.

python -m PyInstaller SC_Localizer.spec --noconfirm --distpath "%~dp0dist" --workpath "%~dp0build" --clean

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Сборка не удалась.
    echo Чаще всего причина - файлы папки dist кем-то заняты:
    echo   - открыт браузер со страницей программы  =^> закрой вкладку
    echo   - открыт проводник на папке dist         =^> закрой окно
    echo   - антивирус сканирует свежий exe          =^> подожди и запусти снова
    echo.
    pause
    exit /b 1
)

copy /Y "..\SC_LOCALIZER\overrides.ini" "%~dp0dist\SC_Localizer\overrides.ini" >nul 2>&1
copy /Y "%~dp0ПРОЧТИ МЕНЯ.txt" "%~dp0dist\SC_Localizer\ПРОЧТИ МЕНЯ.txt" >nul 2>&1
REM PyInstaller сносит папку dist целиком, поэтому файлы для человека
REM кладём обратно после каждой сборки.

echo.
echo ==========================================
echo   Готово! Папка для раздачи: dist\SC_Localizer
echo ==========================================
echo.
pause
