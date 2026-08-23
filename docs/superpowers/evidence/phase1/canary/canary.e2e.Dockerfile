FROM hub.kce.ksyun.com/cbd-serverless/python:3.12-slim
ARG KSADK_SOURCE_COMMIT=unknown
ENV KSADK_RUNTIME_IMAGE_SOURCE_COMMIT=${KSADK_SOURCE_COMMIT}
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir fastapi uvicorn asyncpg cryptography httpx
EXPOSE 8080
CMD ["python","tests/phase1/canary_app.py"]
