"""Kubernetes backend: Sandbox / SandboxSnapshot custom resources, reconciled by the forge operator.

The worker only talks to the API server: it creates a Sandbox, waits for it to be Running, and execs into its
pod. It needs RBAC for the two resources plus pods/exec, nothing else: no Docker socket, no privileges.

A snapshot captures each node's `volume` directory (a CSI VolumeSnapshot), not the whole filesystem: for the
agent sandbox that is /repo, which holds everything an agent's work consists of. Anything an agent changes
outside it (say, site-packages) is reported by `diff()` but does not survive a restore, and grading always
starts from a clean sandbox anyway.
"""

from __future__ import annotations

import re
import shlex
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from .. import config
from .base import Node, Sandbox, wrap

GROUP, VERSION = "sandbox.forge.dev", "v1alpha1"
CREATED_BY = {"app.kubernetes.io/created-by": "forge"}
POLL = 0.25
CHUNK = 1 << 16  # stdin websocket frame size
_IN_CLUSTER_NS = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
_DIFF_SKIP = ("/proc", "/sys", "/dev", "/run", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf")
_MISSING = 44  # exit code `read` uses for "no such file"

_loaded = False
_load_lock = threading.Lock()


def _load() -> None:
    global _loaded
    with _load_lock:
        if not _loaded:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            _loaded = True


def _crd() -> k8s.CustomObjectsApi:
    _load()
    return k8s.CustomObjectsApi()


def _own_namespace() -> str | None:
    return _IN_CLUSTER_NS.read_text().strip() if _IN_CLUSTER_NS.exists() else None


def namespace() -> str:
    return config.SANDBOX_NAMESPACE or _own_namespace() or "default"


def _dns(s: str, limit: int) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s[:limit].rstrip("-")


def _label_value(v: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", v)[:63].strip("-_.")


def _quantity(docker_size: str) -> str:
    """Docker's '1g' / '512m' -> Kubernetes' '1Gi' / '512Mi'."""
    m = re.fullmatch(r"(\d+)([kmg]?)b?", docker_size.lower())
    return f"{m[1]}{ {'k': 'Ki', 'm': 'Mi', 'g': 'Gi', '': ''}[m[2]] }" if m else docker_size


def _get(plural: str, name: str) -> dict[str, Any] | None:
    try:
        return _crd().get_namespaced_custom_object(GROUP, VERSION, namespace(), plural, name)  # ty: ignore[invalid-return-type]
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _delete(plural: str, name: str, wait: float = 0) -> None:
    try:
        _crd().delete_namespaced_custom_object(GROUP, VERSION, namespace(), plural, name)
    except ApiException as e:
        if e.status != 404:
            raise
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and _get(plural, name) is not None:
        time.sleep(POLL)


def _wait(plural: str, name: str, done: set[str], timeout: float) -> dict[str, Any]:
    """Poll until status.phase is in `done`; raise on Failed or timeout."""
    deadline, delay = time.monotonic() + timeout, POLL
    while True:
        obj = _get(plural, name)
        status = (obj or {}).get("status") or {}
        if status.get("phase") in done:
            return obj  # ty: ignore[invalid-return-type]
        if obj is None or status.get("phase") == "Failed":
            raise RuntimeError(f"{plural}/{name}: {status.get('message') or 'deleted'}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{plural}/{name} still {status.get('phase')}: {status.get('message', '')}")
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)


# ---------- sandboxes ----------
class KubeSandbox(Sandbox):
    def __init__(self, name: str, pod: str, nodes: list[str], started: float) -> None:
        self.id = name
        self.pod = pod
        self.nodes = nodes
        self.started = started  # main container start (epoch seconds): diff() reports what changed after it

    def _exec(
        self, argv: list[str], node: str | None, stdin: bytes | None = None, timeout: float = 120
    ) -> tuple[int, bytes, bytes]:
        _load()
        # kubernetes.stream swaps the transport of the ApiClient it is given while it runs, so every exec gets
        # its own client: sharing one across threads would send concurrent REST calls over a websocket.
        api = k8s.CoreV1Api(k8s.ApiClient())
        ws = stream(
            api.connect_get_namespaced_pod_exec,
            self.pod,
            namespace(),
            container=node or self.nodes[0],
            command=argv,
            stdin=stdin is not None,
            stdout=True,
            stderr=True,
            tty=False,
            binary=True,
            _preload_content=False,
        )
        out, err = bytearray(), bytearray()
        try:
            if stdin is not None:
                for i in range(0, len(stdin), CHUNK):
                    ws.write_stdin(stdin[i : i + CHUNK])
                ws.close_channel(0)  # EOF where the server supports it; callers never rely on it
            deadline = time.monotonic() + timeout
            while ws.is_open():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"exec in {self.pod} exceeded {timeout:.0f}s")
                ws.update(timeout=1)
                out += ws.read_stdout(timeout=0) or b""
                err += ws.read_stderr(timeout=0) or b""
            out += ws.read_stdout(timeout=0) or b""
            err += ws.read_stderr(timeout=0) or b""
            return ws.returncode or 0, bytes(out), bytes(err)
        finally:
            ws.close()
            api.api_client.close()

    def exec(
        self, cmd: str, timeout: float = 120, workdir: str = "/repo", node: str | None = None, shell: str = "bash"
    ) -> tuple[int, str]:
        script = f"cd {shlex.quote(workdir)} 2>/dev/null; {wrap(cmd, timeout, shell)}"
        rc, out, err = self._exec(["sh", "-c", script], node, timeout=timeout + 30)
        return rc, (out + err).decode("utf-8", "replace")

    def put_tar(self, data: bytes, dest: str = "/repo", node: str | None = None) -> None:
        # `head -c` reads exactly the archive, so this never depends on stdin EOF reaching the process
        d = shlex.quote(dest)
        rc, _, err = self._exec(["sh", "-c", f"mkdir -p {d} && head -c {len(data)} | tar -xf - -C {d}"], node, data)
        if rc:
            raise RuntimeError(f"put_tar {dest}: {err.decode('utf-8', 'replace')[-500:]}")

    def read(self, path: str, node: str | None = None) -> str | None:
        p = shlex.quote(path)
        rc, out, err = self._exec(["sh", "-c", f"[ -f {p} ] || exit {_MISSING}; cat -- {p}"], node)
        if rc == _MISSING:
            return None
        if rc:
            raise RuntimeError(f"read {path}: {err.decode('utf-8', 'replace')[-500:]}")
        return out.decode("utf-8", "replace")

    def diff(self) -> list[tuple[str, str]]:
        # ctime (unlike mtime) can't be set back from inside a container, so `touch -d` can't hide a change
        prune = " -o ".join(f"-path {p}" for p in _DIFF_SKIP)
        cmd = f"find / -xdev \\( {prune} \\) -prune -o -newerct @{int(self.started)} -print 2>/dev/null"
        _, out, _ = self._exec(["sh", "-c", cmd], None, timeout=120)
        return [("C", p) for p in out.decode("utf-8", "replace").splitlines() if p and p != "/"]

    def snapshot(self, repo: str, tag: str) -> tuple[str, float]:
        t0 = time.perf_counter()
        name = ref(repo, tag)
        for node in self.nodes:  # a volume snapshot only sees what reached the disk: flush page caches first
            self._exec(["sync"], node, timeout=60)
        body: dict[str, Any] = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "SandboxSnapshot",
            "metadata": {"name": name, "labels": {**CREATED_BY, "forge.dev/repo": _label_value(repo)}},
            "spec": {"sandboxName": self.id},
        }
        if config.SANDBOX_SNAPSHOT_CLASS:
            body["spec"]["volumeSnapshotClassName"] = config.SANDBOX_SNAPSHOT_CLASS
        try:
            _crd().create_namespaced_custom_object(GROUP, VERSION, namespace(), "sandboxsnapshots", body)
        except ApiException as e:
            if e.status != 409:
                raise
            _delete("sandboxsnapshots", name, wait=60)  # same repo+tag: replace, like re-tagging an image
            _crd().create_namespaced_custom_object(GROUP, VERSION, namespace(), "sandboxsnapshots", body)
        # Cut is enough to go on: the point-in-time copy exists, so the sandbox may keep writing.
        _wait("sandboxsnapshots", name, {"Cut", "Ready"}, config.SANDBOX_START_TIMEOUT)
        return name, (time.perf_counter() - t0) * 1000

    def destroy(self) -> None:
        try:
            _delete("sandboxes", self.id)
        except Exception:  # best effort: the sandbox's TTL cleans up whatever this misses
            pass


def _owner() -> list[dict[str, Any]]:
    """Tie sandboxes to the worker pod that made them, so they go away with it (same namespace only)."""
    if config.POD_NAME and config.POD_UID and _own_namespace() == namespace():
        return [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "name": config.POD_NAME,
                "uid": config.POD_UID,
                "blockOwnerDeletion": False,
            }
        ]
    return []


def _container(n: Node) -> dict[str, Any]:
    limits = {"memory": _quantity(config.SANDBOX_MEM), "cpu": str(config.SANDBOX_CPUS)}
    c: dict[str, Any] = {
        "name": n.name,
        "image": n.image,
        "env": [{"name": k, "value": v} for k, v in n.env.items()],
        "resources": {"limits": limits, "requests": limits},
    }
    if n.command:
        c["command"] = list(n.command)
    if n.volume:
        c["volume"] = {"mountPath": n.volume, "size": config.SANDBOX_DISK}
    return c


def _started(pod_name: str, container: str) -> float:
    """When the container started (epoch seconds), per the kubelet."""
    pod = k8s.CoreV1Api().read_namespaced_pod(pod_name, namespace())
    for cs in (pod.status.container_statuses if pod.status else None) or []:
        running = cs.state.running if cs.state else None
        if cs.name == container and running and isinstance(running.started_at, datetime):
            return running.started_at.timestamp()
    return time.time()


def create(ref: str | None, *, nodes: list[Node], labels: dict[str, str], isolated: bool) -> KubeSandbox:
    name = f"forge-{uuid.uuid4().hex[:10]}"
    spec: dict[str, Any] = {
        "network": "None" if isolated else "Cluster",
        "ttl": f"{config.SANDBOX_TTL_SECONDS}s",
    }
    if ref is None:
        spec["containers"] = [_container(n) for n in nodes]
    else:
        spec["snapshotRef"] = {"name": ref}
    if config.SANDBOX_RUNTIME_CLASS:
        spec["runtimeClassName"] = config.SANDBOX_RUNTIME_CLASS
    if config.SANDBOX_STORAGE_CLASS:
        spec["storageClassName"] = config.SANDBOX_STORAGE_CLASS
    meta: dict[str, Any] = {
        "name": name,
        "labels": {**CREATED_BY, **{f"forge.dev/{k}": _label_value(v) for k, v in labels.items()}},
    }
    if owner := _owner():
        meta["ownerReferences"] = owner
    body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Sandbox", "metadata": meta, "spec": spec}
    _crd().create_namespaced_custom_object(GROUP, VERSION, namespace(), "sandboxes", body)
    try:
        obj = _wait("sandboxes", name, {"Running"}, config.SANDBOX_START_TIMEOUT)
        status = obj["status"]
        template = [c["name"] for c in status["template"]]
        started = _started(status["podName"], template[0])
        return KubeSandbox(name, status["podName"], template, started)
    except BaseException:
        _delete("sandboxes", name)
        raise


def attach(sandbox_id: str) -> KubeSandbox:
    obj = _get("sandboxes", sandbox_id)
    status = (obj or {}).get("status") or {}
    if status.get("phase") != "Running":
        raise LookupError(f"sandbox {sandbox_id} is not running ({status.get('phase') or 'not found'})")
    return KubeSandbox(sandbox_id, status["podName"], [c["name"] for c in status["template"]], time.time())


# ---------- snapshots ----------
def ref(repo: str, tag: str) -> str:
    return _dns(f"{repo}-{tag}", 200)


def ensure_base() -> None:
    """Nothing to do: images are built by CI and pulled by the kubelet."""


def ensure_image(image: str, context: str) -> None:
    """Nothing to do: images are built by CI and pulled by the kubelet."""


def base_id() -> str:
    return config.SANDBOX_IMAGE  # deploy it by digest or an immutable tag


def exists(ref: str) -> bool:
    snap = _get("sandboxsnapshots", ref)
    phase = ((snap or {}).get("status") or {}).get("phase")
    if phase == "Failed":
        _delete("sandboxsnapshots", ref)
    return phase in ("Cut", "Ready")


def delete_snapshot(ref: str) -> None:
    _delete("sandboxsnapshots", ref)


def gc(snapshots: bool = False) -> tuple[int, int]:
    selector = ",".join(f"{k}={v}" for k, v in CREATED_BY.items())

    def wipe(plural: str) -> int:
        items = _crd().list_namespaced_custom_object(GROUP, VERSION, namespace(), plural, label_selector=selector)
        for item in items["items"]:  # ty: ignore[not-subscriptable]
            _delete(plural, item["metadata"]["name"])
        return len(items["items"])  # ty: ignore[not-subscriptable]

    return wipe("sandboxes"), wipe("sandboxsnapshots") if snapshots else 0
