FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/swebench/requirements.txt /tmp/evaluation-requirements.txt
RUN pip install --no-cache-dir -r /tmp/evaluation-requirements.txt uv
# A relocatable Python distribution independent of the target project's Python.
ENV UV_PYTHON_INSTALL_DIR=/opt/agent-python
RUN uv python install 3.12 \
    && uv venv --python 3.12 --managed-python /opt/agent-venv
RUN python -c "import urllib.request,tarfile,io,pathlib; b=urllib.request.urlopen('https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz').read(); t=tarfile.open(fileobj=io.BytesIO(b)); p=pathlib.Path('/opt/agent-tools/rg'); p.parent.mkdir(); p.write_bytes(t.extractfile('ripgrep-14.1.1-x86_64-unknown-linux-musl/rg').read()); p.chmod(0o755)"
COPY pyproject.toml /opt/codehead/pyproject.toml
COPY src /opt/codehead/src
RUN uv pip install --python /opt/agent-venv/bin/python /opt/codehead \
    && pip freeze > /opt/control-requirements.lock \
    && uv pip freeze --python /opt/agent-venv/bin/python > /opt/agent-requirements.lock
COPY scripts /opt/driver/scripts
# SWE-bench 5.0.2 uses the CLI when stopping/removing scoring containers.
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/opt/driver
WORKDIR /results
ENTRYPOINT ["python", "-m", "scripts.swebench.control"]
