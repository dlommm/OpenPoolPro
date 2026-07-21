FROM python:3.12-slim AS css
WORKDIR /build
RUN pip install --no-cache-dir pytailwindcss && tailwindcss --help >/dev/null 2>&1 || true
COPY tailwind.css ./
COPY css ./css
COPY app/templates ./app/templates
RUN tailwindcss -i tailwind.css -o app.css --minify

FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY --from=css /build/app.css ./app/static/app.css
ARG APP_VERSION=dev
ENV DATA_DIR=/data RUN_SCHEDULER=1 APP_VERSION=$APP_VERSION
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
