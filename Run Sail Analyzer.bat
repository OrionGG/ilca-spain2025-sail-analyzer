@echo off
REM ---- ILCA Sail Analyzer launcher (Windows) ----
REM Double-click this file to start the local web app. It opens in your browser.
cd /d "%~dp0sail_analyzer"
echo Starting Sail Analyzer...  (first run parses the KML files, ~20s)
python app.py
pause
