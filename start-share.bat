@echo off
rem Same as start.bat, but other devices on the same network (phone, another PC) can open the page too.
call "%~dp0start.bat" --share %*
