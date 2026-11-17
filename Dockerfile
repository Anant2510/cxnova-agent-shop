# CX Nova Agent Shop (Coforge Agentic Framework): storefront + console + API + MCP + A2A (open http://<host>:8080/)
# docker build -t coforge-agentic-framework .            (add --build-arg WITH_BROWSER=1 for Playwright/Chromium)
FROM python:3.12-slim

ARG WITH_BROWSER=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CAF_HOME=/app CAF_RUNS_DIR=/data/runs CAF_DATA_DIR=/data/shop

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY sample_data ./sample_data
COPY console/dist ./console/dist

RUN pip install --no-cache-dir -e ".[anthropic,openai,mcp]" \
 && if [ "$WITH_BROWSER" = "1" ]; then pip install --no-cache-dir playwright && python -m playwright install --with-deps chromium; fi

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1
CMD ["caf", "serve", "--host", "0.0.0.0", "--port", "8080"]
