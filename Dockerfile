# agentic-bot web UI in a container. The agent's run_command executes inside this container,
# so the container boundary is the sandbox: mount only the workspace volume you want it to touch.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 agent
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN mkdir -p /data && chown -R agent:agent /app /data
USER agent
# Requests arrive from the Docker bridge, not loopback, so the app must run in exposed mode with a password.
ENV AGENTIC_WORKSPACE_DIR=/data/workspaces AGENTIC_DEPLOYMENT_MODE=exposed AGENTIC_SANDBOX_BACKEND=subprocess AGENTIC_IN_CONTAINER=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/key_status')" || exit 1
# Platform hosts (Cloud Run, Fly, FreeMilk) route to $PORT, so the server must listen there; 8080
# matches the EXPOSE above for a plain `docker run`/compose. `exec` keeps Flask as PID 1 for signals.
CMD ["sh", "-c", "exec python webui.py --host 0.0.0.0 --port ${PORT:-8080}"]
