FROM hub.kce.ksyun.com/cbd-serverless/python:3.12-slim
ARG KSADK_SOURCE_COMMIT=unknown
ENV KSADK_RUNTIME_IMAGE_SOURCE_COMMIT=${KSADK_SOURCE_COMMIT}
WORKDIR /app
# The durability harness exercises the Kernel/Session/runtime contracts only.
# Installing KsADK with its complete default dependency graph pulled OCR,
# OpenCV, LangChain and OTLP into every canary rebuild.  Keep the image honest
# and reproducible while making the Makefile/CI loop incremental: install the
# exact harness runtime, then the checked-out package without optional deps.
RUN pip install --no-cache-dir \
      fastapi uvicorn asyncpg cryptography httpx jsonschema
COPY . .
RUN pip install --no-cache-dir --no-deps -e .
EXPOSE 8080
CMD ["python","tests/phase1/canary_app.py"]
