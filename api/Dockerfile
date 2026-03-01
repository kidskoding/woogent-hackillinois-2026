FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Install dependencies (no editable install needed — app is run directly)
RUN uv pip install --system --no-cache \
    fastapi==0.115.6 \
    "uvicorn[standard]==0.34.0" \
    sqlalchemy==2.0.36 \
    aiomysql==0.2.0 \
    pydantic==2.10.4 \
    pydantic-settings==2.7.1 \
    "python-jose[cryptography]==3.3.0" \
    cryptography==43.0.3 \
    python-multipart==0.0.20 \
    httpx==0.28.1 \
    stripe==14.4.0

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
