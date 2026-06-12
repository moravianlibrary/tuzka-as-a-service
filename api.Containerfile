FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml .
# Install deps, then strip runtime-unused tooling (pip/wheel) and all bytecode —
# Python recompiles what it needs on first import. Keeps setuptools so the stray
# `pkg_resources` import some libs still do at runtime keeps working.
RUN pip install --no-cache-dir ".[api]" \
 && pip uninstall -y pip wheel 2>/dev/null || true \
 && find /usr/local/lib/python3.13 -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /usr/local/lib/python3.13 -name '*.pyc' -delete

COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
