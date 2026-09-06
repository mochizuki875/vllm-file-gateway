FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-crosextra-carlito \
        fonts-noto-cjk \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /var/lib/file-gateway \
    && chown -R gateway:gateway /var/lib/file-gateway

USER gateway
ENV GATEWAY_DATA_DIR=/var/lib/file-gateway
EXPOSE 8080

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]