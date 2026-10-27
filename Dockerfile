# syntax=docker/dockerfile:1

FROM python:3.13-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=build /install /usr/local
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY knowledge ./knowledge

# Never run as root inside the container.
RUN useradd --system --uid 10001 supportflow && chown -R supportflow /srv
USER supportflow

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# The web server. Run the worker from the same image with: python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
