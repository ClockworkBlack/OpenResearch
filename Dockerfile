# Remote MCP server for Claude custom connectors and other MCP clients.
#   docker build -t openresearch .
#   docker run -p 8765:8765 -e OPENRESEARCH_TOKEN=choose-a-long-secret openresearch
# Connector URL: https://<your-host>/mcp/<that secret>
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY openresearch ./openresearch
# Playwright with Chromium's headless shell (smaller than full Chromium) for JavaScript-built pages.
# Set OPENRESEARCH_RENDER=never on the host to switch rendering off without rebuilding.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir ".[search,pdf]" "playwright==1.63.0" \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache
ENV HOST=0.0.0.0 PORT=8765 OPENRESEARCH_CACHE_DIR=/tmp/openresearch OPENRESEARCH_LOG_STDERR=1
EXPOSE 8765
CMD ["python", "-m", "openresearch", "mcp", "--http"]
