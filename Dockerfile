FROM mirror.gcr.io/library/python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp cryptography
COPY proxy/ /app/proxy/
# The daemon builds RFC 0020 evidence bundles from the SAME schema the verifier parses, so it
# needs verify/bundle.py. Only the schema (pure dataclasses) and the lazy package __init__ are
# copied — deliberately NOT facts.py, so the verifier's dependencies never enter the attested
# image and a change to the verifier cannot alter this image's measurement.
COPY verify/__init__.py verify/bundle.py /app/verify/
WORKDIR /app
ARG GIT_COMMIT
ENV DAEMON_COMMIT=$GIT_COMMIT
EXPOSE 8080
ENTRYPOINT ["python", "-m", "proxy.main"]
