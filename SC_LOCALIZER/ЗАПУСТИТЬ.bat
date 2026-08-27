@echo off
REM Файл сохранён в кодировке cp866, иначе cmd.exe не понимает русский текст.
cd /d "%~dp0"

echo ======================================
echo    SC Localizer
echo ======================================
echo.

python --version >nul 2>&1
if errorlevel 1 goto nopython

REM Порт может быть занят прошлым запуском. Без этой проверки python молча
REM падает с ошибкой привязки, браузер открывает старый сервер, и человек
REM видит неработающие кнопки, не понимая почему.
netstat -ano -p tcp | findstr /r /c:"LISTENING" | findstr /c:":5000 " >nul 2>&1
if not errorlevel 1 goto busy

python -c "import flask, dotenv" >nul 2>&1
if errorlevel 1 goto install
goto run

:install
echo Первый запуск, устанавливаю библиотеки. Подожди полминуты...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 goto nopip
echo Готово.
echo.
goto run

:run
echo Адрес: http://127.0.0.1:5000
echo Браузер откроется сам через несколько секунд.
echo.
echo Чтобы ОСТАНОВИТЬ, закрой это окно.
echo.
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000"
python app.py
echo.
echo Сервер остановлен.
pause
exit /b 0

:busy
echo [ВНИМАНИЕ] Порт 5000 уже занят - программа запущена в другом окне.
echo.
echo Открываю уже запущенную: http://127.0.0.1:5000
echo Если кнопки не работают, нажми в браузере Ctrl+F5.
echo.
echo Чтобы запустить заново - закрой то окно и запусти этот файл снова.
start http://127.0.0.1:5000
pause
exit /b 0

:nopython
echo [ОШИБКА] Python не найден.
echo Установи Python с сайта python.org и отметь галочку "Add to PATH".
pause
exit /b 1

:nopip
echo [ОШИБКА] Не удалось установить библиотеки.
echo Проверь интернет и запусти снова.
pause
exit /b 1
