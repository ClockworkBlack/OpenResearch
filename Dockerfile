# Remote MCP server for Claude custom connectors and other MCP clients.
#   docker build -t openresearch .
#   docker run -p 8765:8765 -e OPENRESEARCH_TOKEN=choose-a-long-secret openresearch
# Connector URL: https://<your-host>/mcp/<that secret>
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY openresearch ./openresearch
RUN pip install --no-cache-dir ".[search,pdf]"
ENV HOST=0.0.0.0 PORT=8765 OPENRESEARCH_CACHE_DIR=/tmp/openresearch
EXPOSE 8765
CMD ["python", "-m", "openresearch", "mcp", "--http"]
