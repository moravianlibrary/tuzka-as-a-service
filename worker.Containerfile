FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
# CMD overridden per worker in docker-compose.yml
