FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] needs libpq's runtime shared library even with the
# binary wheel (the wheel bundles the C extension, not libpq itself on
# all base images) - installed via apt rather than relying on the wheel
# alone, matching the caution deploy.sh's own comments show about
# platform-specific psycopg behavior (Bundle R bug #4).
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY contract/ contract/

EXPOSE 8000

CMD ["uvicorn", "src.platform.app:app", "--host", "0.0.0.0", "--port", "8000"]
