FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home app
COPY app ./app
COPY prompts ./prompts
COPY seed ./seed
COPY tests ./tests
COPY pytest.ini .
RUN mkdir -p /app/data && chown -R app:app /app
USER app
CMD ["python", "-m", "app.main", "v2-daemon"]
