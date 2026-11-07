FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "psycopg[binary,pool]>=3.2" "httpx>=0.27" "pydantic-settings>=2.4" "PyJWT>=2.9" "cryptography>=43"
COPY app ./app
COPY migrations ./migrations
COPY demo ./demo
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
