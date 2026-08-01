FROM node:24-slim AS ui-build

WORKDIR /ui
# ui-next/ is the projection-driven authority-transition workbench (the deployed
# SPA). The older ui/ (public-replay demo) is not shipped.
COPY ui-next/package.json ui-next/package-lock.json ./
RUN npm ci
COPY ui-next/ ./
# NO build-time credential is passed into the SPA. Auth is the Cognito session
# cookie (httpOnly, set by POST /api/login and validated server-side), so the
# browser never holds a token. A VITE_DEMO_TOKEN ARG/ENV used to be inlined into
# the bundle here — Vite bakes VITE_* into the shipped JS, which makes any such
# value a PUBLISHED credential readable by anyone who loads the page. Do not
# reintroduce one: if the SPA appears to need a token, the session cookie is not
# reaching the request (check `credentials: "same-origin"`).
RUN npm run build


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
COPY --from=ui-build /ui/dist/ /app/ui/

EXPOSE 8000

CMD ["uvicorn", "src.platform.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
