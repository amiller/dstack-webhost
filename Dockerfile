FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp
COPY proxy/ /app/proxy/
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "proxy.main"]
