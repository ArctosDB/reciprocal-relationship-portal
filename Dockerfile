FROM python:3.13.5-slim-bookworm

WORKDIR /app
RUN pip install uv==0.13.8
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY vulpes ./vulpes

ENV PATH="/app/.venv/bin:$PATH"
ENV OTEL_SERVICE_NAME=arctos-vulpes-backend
ENV OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true

RUN useradd --system --no-create-home vulpes
USER vulpes
EXPOSE 8000

CMD ["opentelemetry-instrument", "uvicorn", "vulpes.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--log-config", "vulpes/config/logging.json"]
