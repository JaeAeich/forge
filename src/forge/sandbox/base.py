"""The backend-neutral sandbox interface."""

from __future__ import annotations

import io
import shlex
import tarfile
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Self

from .. import config


@dataclass(frozen=True)
class Node:
    """One container of a sandbox. Nodes of the same sandbox reach each other by name.

    `volume` is the directory whose contents a snapshot must keep. Docker snapshots the whole filesystem
    anyway; Kubernetes puts this directory on its own PersistentVolume and snapshots only that."""

    name: str
    image: str
    command: Sequence[str] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    volume: str | None = None


def main_node() -> Node:
    """The agent sandbox: the repo under test lives in /repo."""
    return Node("main", config.SANDBOX_IMAGE, ("sleep", "infinity"), volume="/repo")


def make_tar(files: Mapping[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name.lstrip("/"))
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def wrap(cmd: str, timeout: float, shell: str = "bash") -> str:
    """Run `cmd` under a hard timeout (SIGKILL 5s after SIGTERM), stderr folded into stdout."""
    return f"timeout -k 5 {int(timeout)} {shell} -c {shlex.quote(cmd)} 2>&1"


class Sandbox(ABC):
    """A running sandbox. Use as a context manager, or call destroy()."""

    id: str
    nodes: list[str]

    @classmethod
    def create(
        cls,
        ref: str | None = None,
        *,
        nodes: Sequence[Node] | None = None,
        labels: Mapping[str, str] | None = None,
        isolated: bool = True,
    ) -> Sandbox:
        """A fresh sandbox from `nodes` (default: the agent sandbox), or a restore of snapshot `ref`.

        isolated: no network access beyond the sandbox's own nodes."""
        from . import _backend

        if ref == config.SANDBOX_IMAGE:  # the base image is a valid "snapshot" of nothing
            ref = None
        return _backend().create(ref, nodes=list(nodes or [main_node()]), labels=dict(labels or {}), isolated=isolated)

    @classmethod
    def attach(cls, sandbox_id: str) -> Sandbox:
        """Reconnect to a running sandbox by id, e.g. from another API process."""
        from . import _backend

        return _backend().attach(sandbox_id)

    @abstractmethod
    def exec(
        self, cmd: str, timeout: float = 120, workdir: str = "/repo", node: str | None = None, shell: str = "bash"
    ) -> tuple[int, str]:
        """Run a shell command; returns (exit code, combined stdout+stderr)."""

    @abstractmethod
    def put_tar(self, data: bytes, dest: str = "/repo", node: str | None = None) -> None:
        """Extract a tar archive into `dest` (created if missing)."""

    @abstractmethod
    def read(self, path: str, node: str | None = None) -> str | None:
        """A file's text, or None if it doesn't exist."""

    @abstractmethod
    def diff(self) -> list[tuple[str, str]]:
        """Paths changed in the main node's filesystem since it started: [(kind, path)], kind in A/C/D."""

    @abstractmethod
    def snapshot(self, repo: str, tag: str) -> tuple[str, float]:
        """Capture every node. Returns (ref, ms). The sandbox keeps running and may be written to right after.

        Same repo+tag twice replaces the earlier snapshot (retries are idempotent)."""

    @abstractmethod
    def destroy(self) -> None:
        """Remove the sandbox. Never raises."""

    def put(self, files: Mapping[str, bytes | str], dest: str = "/", node: str | None = None) -> None:
        self.put_tar(make_tar(files), dest, node)

    def patch(self) -> str:
        """The agent's change vs the task's base commit (the root commit), robust to the agent committing.

        --no-renames: a move shows as delete + add, so a file renamed into conftest.py counts as new there."""
        # test-run artifacts (coverage data, bytecode) aren't part of the agent's change
        _, out = self.exec(
            "git add -A -- . ':(exclude).coverage' ':(exclude)**/__pycache__/**' ':(exclude)*.pyc' >/dev/null 2>&1; "
            "git diff --cached --binary --no-renames $(git rev-list --max-parents=0 HEAD | tail -1)"
        )
        return out

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()
