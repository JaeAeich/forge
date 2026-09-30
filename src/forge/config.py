"""All configuration, read once from environment variables prefixed `FORGE_` (or a `.env` file).

Validated and typed by pydantic; module-level names are what the rest of the code imports.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env", extra="ignore")

    # infrastructure
    data: Path = Path("data")
    database_url: str = "postgresql://forge:forge@localhost:5433/forge"
    db_pool_size: int = Field(20, ge=1)
    nats_url: str = "nats://localhost:4222"
    ollama_url: str = "http://localhost:11434"
    log_format: str = Field("text", pattern="^(text|json)$")
    log_level: str = "INFO"

    # models
    model: str = "qwen2.5-coder:14b"
    embed_model: str = "embeddinggemma"
    llm_concurrency: int = Field(2, ge=1)
    num_ctx: int = 16384

    # sandboxes
    sandbox_backend: Literal["docker", "kubernetes"] = "docker"
    sandbox_image: str = "forge-sandbox:latest"
    env_app_image: str = "forge-env-app:latest"
    sandbox_mem: str = "1g"
    sandbox_cpus: float = 1.0
    sandbox_ttl_seconds: int = Field(7200, ge=60)  # a sandbox whose worker died is deleted after this
    sandbox_start_timeout: float = 300.0
    registry: str = ""  # docker: e.g. "registry:5000"; shares task images and step snapshots between hosts
    # kubernetes backend (see operator/)
    sandbox_namespace: str = ""  # default: the worker's own namespace
    sandbox_runtime_class: str = ""  # e.g. gvisor: agent code is untrusted
    sandbox_storage_class: str = ""
    sandbox_snapshot_class: str = ""
    sandbox_disk: str = "4Gi"
    pod_name: str = ""  # downward API; sandboxes are owned by (and deleted with) the worker pod
    pod_uid: str = ""

    # pipeline
    max_steps: int = 14
    tasks_per_repo: int = 8
    commits_scanned: int = 400
    forks: int = 1
    workers: int = Field(10, ge=1)
    refresh_seconds: int = 1800
    shutdown_grace_seconds: float = 30.0


settings = Settings()

DATA = settings.data.resolve()
REPO_DIR = DATA / "repos"
DATABASE_URL = settings.database_url
DB_POOL_SIZE = settings.db_pool_size
NATS_URL = settings.nats_url
OLLAMA_URL = settings.ollama_url.rstrip("/")

CODER_MODEL = settings.model
EMBED_MODEL = settings.embed_model
LLM_CONCURRENCY = settings.llm_concurrency
NUM_CTX = settings.num_ctx

SANDBOX_BACKEND = settings.sandbox_backend
SANDBOX_IMAGE = settings.sandbox_image
ENV_APP_IMAGE = settings.env_app_image
SANDBOX_TTL_SECONDS = settings.sandbox_ttl_seconds
SANDBOX_START_TIMEOUT = settings.sandbox_start_timeout
SANDBOX_NAMESPACE = settings.sandbox_namespace
SANDBOX_RUNTIME_CLASS = settings.sandbox_runtime_class
SANDBOX_STORAGE_CLASS = settings.sandbox_storage_class
SANDBOX_SNAPSHOT_CLASS = settings.sandbox_snapshot_class
SANDBOX_DISK = settings.sandbox_disk
POD_NAME = settings.pod_name
POD_UID = settings.pod_uid
SANDBOX_MEM = settings.sandbox_mem
SANDBOX_CPUS = settings.sandbox_cpus
REGISTRY = settings.registry.rstrip("/")

MAX_STEPS = settings.max_steps
TASKS_PER_REPO = settings.tasks_per_repo
COMMITS_SCANNED = settings.commits_scanned
FORKS_PER_ROLLOUT = settings.forks
WORKERS = settings.workers
REFRESH_SECONDS = settings.refresh_seconds

# Small, pure-python repos with pytest suites. Their extra deps are baked into images/sandbox.Dockerfile.
REPOS = {
    "more-itertools": "https://github.com/more-itertools/more-itertools",
    "toolz": "https://github.com/pytoolz/toolz",
    "cachetools": "https://github.com/tkem/cachetools",
    "boltons": "https://github.com/mahmoud/boltons",
    "parse": "https://github.com/r1chardj0n3s/parse",
    "jmespath": "https://github.com/jmespath/jmespath.py",
    "tinydb": "https://github.com/msiemens/tinydb",
    "funcy": "https://github.com/Suor/funcy",
    "click": "https://github.com/pallets/click",
    "marshmallow": "https://github.com/marshmallow-code/marshmallow",
    "schedule": "https://github.com/dbader/schedule",
}
