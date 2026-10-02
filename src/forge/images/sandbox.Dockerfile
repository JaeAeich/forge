# Base image for agent sandboxes: the repo under test is copied to /repo at task creation.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==8.3.4 pytest-cov packaging typing_extensions pytz python-dateutil simplejson
RUN git config --global user.email forge@local && git config --global user.name forge \
    && git config --global init.defaultBranch main && git config --global --add safe.directory '*'
WORKDIR /repo
CMD ["sleep", "infinity"]
