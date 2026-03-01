FROM python:3.12-slim

# Обновляем систему и ставим ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Копируем проект
WORKDIR /app
COPY . /app

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Команда запуска
CMD ["python", "main.py"]