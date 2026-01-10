FROM python:3.10-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y openssl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем requirements
COPY requirements.txt /app/

# 1. Обновляем pip
# 2. Устанавливаем зависимости с увеличенным таймаутом
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --default-timeout=100 -r requirements.txt

# Создаем папку для сертификатов
RUN mkdir -p /app/certs

# Копируем код
COPY backend/ /app/backend/
RUN mkdir -p /app/backend/frontend
COPY frontend/ /app/backend/frontend

# Копируем скрипт запуска
COPY docker/start.sh /app/
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]