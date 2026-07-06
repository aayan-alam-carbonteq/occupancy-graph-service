FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV GRAPH_SERVICE_DB=/data/graph.sqlite
EXPOSE 8000
# host + port fixed for the container; DB comes from the mounted volume via env
CMD ["sh", "-c", "occupancy-graph-serve --db \"$GRAPH_SERVICE_DB\" --host 0.0.0.0 --port 8000"]
