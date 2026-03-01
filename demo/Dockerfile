FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

RUN uv pip install --system --no-cache \
    gradio==5.9.1 \
    google-generativeai==0.8.3 \
    httpx==0.28.1 \
    python-dotenv==1.0.1

COPY . .

EXPOSE 7860

ENV GRADIO_SERVER_NAME=0.0.0.0

CMD ["python", "app.py"]
