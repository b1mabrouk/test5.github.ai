@echo off
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Installing Whisper and dependencies...
pip install openai-whisper
pip install ffmpeg-python
pip install torch
pip install tqdm

echo Checking for FFmpeg installation...
where ffmpeg >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo FFmpeg not found. Please install FFmpeg from https://ffmpeg.org/download.html
    echo After installation, add FFmpeg to your system PATH
    pause
) else (
    echo FFmpeg is already installed.
)

echo Installation complete!
echo To run the application, use: python app.py
pause
