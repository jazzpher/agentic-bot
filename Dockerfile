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
# AGENTIC_TASK_SHELL turns on run_command for durable /task runs: the container boundary IS the sandbox here
# (non-root user, dedicated /data), which is why the subprocess backend is safe to use inside it. Network for
# shell commands stays off. Remove that one variable to keep /task to file tools only.
# AGENTIC_TASK_MAX_ELAPSED raises the elapsed ceiling for those runs: a real debug cycle (run the tests, read
# the failures, fix, run again) took 159-329 s live, so the 300 s default discarded runs that had finished.
# A client still cannot ask for more than this; lower it, or drop it, to keep the stricter default.
ENV AGENTIC_WORKSPACE_DIR=/data/workspaces AGENTIC_DEPLOYMENT_MODE=exposed AGENTIC_SANDBOX_BACKEND=subprocess AGENTIC_IN_CONTAINER=1 \
    AGENTIC_TASK_SHELL=on AGENTIC_TASK_MAX_ELAPSED=600
# Provider defaults for a bare `docker run`: NVIDIA NIM, whose key arrives as AGENTIC_API_KEY. Every value is
# overridable at run time (-e, compose, Cloud Run env), and the UI's own provider settings win over all of them.
ENV AGENTIC_API_URL=https://integrate.api.nvidia.com/v1/chat/completions \
    AGENTIC_MODEL=nvidia/nemotron-3-ultra-550b-a55b
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/key_status')" || exit 1
# Platform hosts (Cloud Run, Fly, FreeMilk) route to $PORT, so the server must listen there; 8080
# matches the EXPOSE above for a plain `docker run`/compose. `exec` keeps Flask as PID 1 for signals.
CMD ["sh", "-c", "exec python webui.py --host 0.0.0.0 --port ${PORT:-8080}"]
