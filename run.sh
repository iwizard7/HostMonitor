#!/usr/bin/env bash

# Host Monitor for macOS - Startup Script
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "🍎 Запуск Host Monitor для macOS..."

# Check Python3
if ! command -v python3 &> /dev/null; then
    echo "❌ Ошибка: Python 3 не найден. Установите Python 3."
    exit 1
fi

# Virtual Environment
if [ ! -d "venv" ]; then
    echo "📦 Создание виртуального окружения (venv)..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt
fi

PORT=8000

# Open browser window
(sleep 1.5 && open "http://localhost:${PORT}") &

echo "🚀 Сервер запущен на http://localhost:${PORT}"
echo "Для завершения работы нажмите Ctrl + C"

./venv/bin/python3 -m uvicorn backend.server:app --host 127.0.0.1 --port ${PORT}
