# The scorer builds this on linux/amd64 and runs it with no network.
# Base image: the digest we were told every commit was labeled in.
# Nothing is downloaded at build time: the selector ships with its map of the
# pinned sqlglot commit (map.json.gz) and that commit's Python sources (sources.zip).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY selector.py map.json.gz sources.zip /app/
RUN chmod a+r /app/selector.py /app/map.json.gz /app/sources.zip
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
