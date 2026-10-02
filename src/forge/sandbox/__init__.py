"""Sandboxes: isolated environments for agent code that can be snapshotted, restored and forked.

Two interchangeable backends, picked by FORGE_SANDBOX_BACKEND:

  docker      containers on the local Docker engine; a snapshot is a `docker commit` image (whole filesystem).
              For laptops and single hosts (several hosts can share snapshots through FORGE_REGISTRY).
  kubernetes  `Sandbox` / `SandboxSnapshot` custom resources run by the forge operator (operator/): a pod per
              sandbox, a PersistentVolume per persisted directory, CSI VolumeSnapshots as snapshots.
              Workers need no Docker socket and no privileges.

Callers only see `Sandbox` and snapshot refs (opaque strings stored in the database): `Sandbox.create(ref)`
restores any ref `sb.snapshot()` returned, on any worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import config
from .base import Node, Sandbox, main_node

if TYPE_CHECKING:
    from types import ModuleType

__all__ = [
    "Node",
    "Sandbox",
    "base_id",
    "delete_snapshot",
    "ensure_base",
    "ensure_image",
    "exists",
    "gc",
    "main_node",
    "ref",
]


def _backend() -> ModuleType:
    if config.SANDBOX_BACKEND == "kubernetes":
        from . import kube

        return kube
    from . import docker_engine

    return docker_engine


def ensure_base() -> None:
    """Make the base sandbox image available (docker builds it locally; kubernetes pulls a prebuilt one)."""
    _backend().ensure_base()


def ensure_image(image: str, context: str) -> None:
    """Build `forge/images/<context>` as `image` if it's missing (docker only; kubernetes pulls prebuilt images)."""
    _backend().ensure_image(image, context)


def base_id() -> str:
    """Identity of the base image. Anything derived from it (task snapshots) is keyed on this."""
    return _backend().base_id()


def ref(repo: str, tag: str) -> str:
    """The ref `sb.snapshot(repo, tag)` produces, without taking one."""
    return _backend().ref(repo, tag)


def exists(ref: str) -> bool:
    """Whether a snapshot can be restored here (docker pulls it from the shared registry if needed)."""
    return _backend().exists(ref)


def delete_snapshot(ref: str) -> None:
    _backend().delete_snapshot(ref)


def gc(snapshots: bool = False) -> tuple[int, int]:
    """Remove every forge sandbox (and optionally every snapshot). Returns (sandboxes, snapshots) removed."""
    return _backend().gc(snapshots)
