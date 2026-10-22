FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[postgres]"

# Непривилегированный пользователь
RUN useradd --create-home realty
USER realty

CMD ["python", "-m", "minsk_realty.collector"]
