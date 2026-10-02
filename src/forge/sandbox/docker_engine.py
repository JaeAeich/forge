"""Docker backend: containers on the local engine, snapshots are `docker commit` images.

No bind mounts, so it also works when forge itself runs in a container against the host socket: files go
in and out as tar archives. A multi-node sandbox gets its own bridge network with each node aliased by name.
Snapshot of several nodes: pause all (no writes in flight anywhere), commit each, unpause.

Refs are image names. Node 0 is committed as the ref itself, node i as `<ref>-<name>`; a restore reads the
container command and environment back from the committed images.
"""

from __future__ import annotations

import io
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from typing import Any

import docker
import docker.errors
from docker.models.containers import Container

from .. import config
from .base import Node, Sandbox, wrap

LABEL = "forge"
SNAPSHOT_REPOS = ("forge-snap", "forge-task", "forge-env-snap")

_client: docker.DockerClient | None = None
_lock = threading.Lock()


def client() -> docker.DockerClient:
    global _client
    with _lock:
        if _client is None:
            _client = docker.from_env(timeout=300)
    return _client


# ---------- images ----------
def build_image(tag: str, context: str) -> None:
    """Build `forge/images/<context>` (a directory with a Dockerfile, or a single `<context>.Dockerfile`)."""
    root = resources.files("forge.images")
    files: dict[str, bytes] = {}
    if root.joinpath(context).is_dir():
        files = {f.name: f.read_bytes() for f in root.joinpath(context).iterdir() if f.is_file()}
    else:
        files = {"Dockerfile": root.joinpath(f"{context}.Dockerfile").read_bytes()}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    client().images.build(fileobj=buf, custom_context=True, tag=tag, rm=True)


def _has_image(ref: str) -> bool:
    try:
        client().images.get(ref)
        return True
    except docker.errors.ImageNotFound:
        return False


def ensure_image(ref: str, context: str) -> None:
    if not _has_image(ref):
        build_image(ref, context)


def ensure_base() -> None:
    ensure_image(config.SANDBOX_IMAGE, "sandbox")


def base_id() -> str:
    return client().images.get(config.SANDBOX_IMAGE).id


def ref(repo: str, tag: str) -> str:
    """Name for an image other workers may need: in the shared registry if one is configured."""
    return f"{config.REGISTRY}/{repo}:{tag}" if config.REGISTRY else f"{repo}:{tag}"


def exists(ref: str) -> bool:
    """Present locally, or pulled from the registry if another worker made it."""
    if _has_image(ref):
        return True
    if not config.REGISTRY:
        return False
    name, tag = ref.rsplit(":", 1)
    try:
        client().images.pull(name, tag=tag)
        return True
    except docker.errors.NotFound:
        return False


def _publish(ref: str) -> None:
    """Push to the shared registry (no-op on a single machine)."""
    if not config.REGISTRY:
        return
    name, tag = ref.rsplit(":", 1)
    for line in client().images.push(name, tag=tag, stream=True, decode=True):
        if "error" in line:
            raise RuntimeError(f"push {ref}: {line['error']}")


def _node_image(ref: str, i: int, name: str) -> str:
    return ref if i == 0 else f"{ref}-{name}"


def delete_snapshot(ref: str) -> None:
    for img in client().images.list(name=ref.rsplit(":", 1)[0]):
        for t in img.tags:
            if t == ref or t.startswith(f"{ref}-"):
                try:
                    client().images.remove(t, force=True)
                except docker.errors.APIError:
                    pass


# ---------- sandboxes ----------
class DockerSandbox(Sandbox):
    def __init__(self, sid: str, containers: dict[str, Container], network: Any = None) -> None:
        self.id = sid
        self.containers = containers  # node name -> container, in node order
        self.nodes = list(containers)
        self.network = network

    @property
    def c(self) -> Container:
        return self.containers[self.nodes[0]]

    def _node(self, node: str | None) -> Container:
        return self.containers[node or self.nodes[0]]

    def exec(
        self, cmd: str, timeout: float = 120, workdir: str = "/repo", node: str | None = None, shell: str = "bash"
    ) -> tuple[int, str]:
        r = self._node(node).exec_run(["sh", "-c", wrap(cmd, timeout, shell)], workdir=workdir, demux=False)
        return r.exit_code, (r.output or b"").decode("utf-8", "replace")

    def put_tar(self, data: bytes, dest: str = "/repo", node: str | None = None) -> None:
        c = self._node(node)
        c.exec_run(["mkdir", "-p", dest])
        c.put_archive(dest, data)

    def read(self, path: str, node: str | None = None) -> str | None:
        try:
            stream, _ = self._node(node).get_archive(path)
        except docker.errors.NotFound:
            return None
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            f = tar.extractfile(tar.getmembers()[0])
            return f.read().decode("utf-8", "replace") if f else None

    def diff(self) -> list[tuple[str, str]]:
        kinds = {0: "C", 1: "A", 2: "D"}
        return [(kinds[d["Kind"]], d["Path"]) for d in (self.c.diff() or [])]

    def snapshot(self, repo: str, tag: str) -> tuple[str, float]:
        t0 = time.perf_counter()
        out = ref(repo, tag)
        targets = [(c, _node_image(out, i, name)) for i, (name, c) in enumerate(self.containers.items())]
        many = len(targets) > 1
        if many:  # freeze every node first so the copies are consistent with each other
            for c, _ in targets:
                c.pause()
        try:
            with ThreadPoolExecutor(len(targets)) as pool:
                list(pool.map(lambda t: self._commit(*t, pause=not many), targets))
        finally:
            if many:
                for c, _ in targets:
                    c.unpause()
        for _, image in targets:
            _publish(image)
        return out, (time.perf_counter() - t0) * 1000

    @staticmethod
    def _commit(c: Container, image: str, pause: bool) -> None:
        name, tag = image.rsplit(":", 1)
        c.commit(repository=name, tag=tag, pause=pause)

    def destroy(self) -> None:
        for c in self.containers.values():
            try:
                c.remove(force=True, v=True)
            except docker.errors.APIError:
                pass
        if self.network is not None:
            try:
                self.network.remove()
            except docker.errors.APIError:
                pass


def create(ref: str | None, *, nodes: list[Node], labels: dict[str, str], isolated: bool) -> DockerSandbox:
    sid = f"forge-{uuid.uuid4().hex[:10]}"
    if ref is not None:
        for i, n in enumerate(nodes):
            if not exists(_node_image(ref, i, n.name)):
                raise LookupError(f"snapshot {ref} not found")
    network = None
    if len(nodes) > 1:  # nodes talk over a private network; `internal` = no route out
        network = client().networks.create(sid, driver="bridge", internal=isolated, labels={LABEL: "1"})
    containers: dict[str, Container] = {}
    try:
        for i, n in enumerate(nodes):
            kw: dict[str, Any] = {
                "detach": True,
                "name": sid if len(nodes) == 1 else f"{sid}-{n.name}",
                "labels": {LABEL: "1", "forge.sandbox": sid, "forge.node": n.name, "forge.index": str(i), **labels},
                "mem_limit": config.SANDBOX_MEM,
                "nano_cpus": int(config.SANDBOX_CPUS * 1e9),
                "pids_limit": 512,
            }
            if ref is None:  # a restore keeps the committed image's command and environment
                kw |= {"environment": dict(n.env), **({"command": list(n.command)} if n.command else {})}
            image = n.image if ref is None else _node_image(ref, i, n.name)
            if network is None:
                c = client().containers.run(image, network_mode="none" if isolated else "bridge", **kw)
            else:
                c = client().containers.create(image, network=sid, **kw)
                network.disconnect(c)
                network.connect(c, aliases=[n.name])
                c.start()
            containers[n.name] = c
    except Exception:
        DockerSandbox(sid, containers, network).destroy()
        raise
    return DockerSandbox(sid, containers, network)


def attach(sandbox_id: str) -> DockerSandbox:
    found = client().containers.list(all=True, filters={"label": f"forge.sandbox={sandbox_id}"})
    if not found:
        raise LookupError(f"sandbox {sandbox_id} not found")
    found.sort(key=lambda c: int(c.labels.get("forge.index", 0)))
    network = None
    if len(found) > 1:
        try:
            network = client().networks.get(sandbox_id)
        except docker.errors.NotFound:
            pass
    return DockerSandbox(sandbox_id, {c.labels.get("forge.node", "main"): c for c in found}, network)


def gc(snapshots: bool = False) -> tuple[int, int]:
    n = 0
    for c in client().containers.list(all=True, filters={"label": LABEL}):
        c.remove(force=True, v=True)
        n += 1
    for net in client().networks.list(filters={"label": LABEL}):
        try:
            net.remove()
        except docker.errors.APIError:
            pass
    m = 0
    if snapshots:
        for repo in SNAPSHOT_REPOS:
            for img in client().images.list(name=ref(repo, "x").rsplit(":", 1)[0]):
                for t in img.tags:
                    try:
                        client().images.remove(t, force=True)
                        m += 1
                    except docker.errors.APIError:
                        pass
    return n, m
