FROM python:3.12-slim

# кириллица в логах и небуферизованный вывод в journalctl/docker logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# зависимости отдельным слоем: пересобираются только при изменении requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY maxbridge/ ./maxbridge/

# .env и сессия MAX подключаются томом, а не копируются в образ:
# секреты не должны попадать в слои
VOLUME ["/app/data"]

# разовый вход в MAX (нужен ввод SMS-кода):
#   docker compose run --rm bridge python -m maxbridge login
CMD ["python", "-m", "maxbridge"]
