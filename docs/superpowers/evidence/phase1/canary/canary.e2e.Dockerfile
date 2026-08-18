FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir fastapi uvicorn asyncpg cryptography httpx
EXPOSE 8080
CMD ["python","tests/phase1/canary_app.py"]
