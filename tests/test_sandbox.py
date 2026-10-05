"""Sandbox backends: pure helpers (no Docker or cluster needed)."""

from forge.sandbox import kube
from forge.sandbox.base import Node, wrap


def test_wrap_quotes_and_times_out() -> None:
    assert wrap("echo 'hi'", 5) == "timeout -k 5 5 bash -c 'echo '\"'\"'hi'\"'\"'' 2>&1"


def test_kube_names_are_dns_safe() -> None:
    assert kube.ref("forge-task", "More_Itertools@abc.1-ff") == "forge-task-more-itertools-abc-1-ff"
    assert len(kube.ref("forge-snap", "x" * 400)) == 200
    assert kube._label_value("a/b:c" * 30).replace("-", "").isalnum()


def test_kube_quantities() -> None:
    assert kube._quantity("1g") == "1Gi"
    assert kube._quantity("512m") == "512Mi"
    assert kube._quantity("2Gi") == "2Gi"


def test_kube_container_spec() -> None:
    c = kube._container(Node("db", "postgres:16", env={"A": "1"}, volume="/pgdata"))
    assert c["env"] == [{"name": "A", "value": "1"}]
    assert c["volume"]["mountPath"] == "/pgdata"
    assert "command" not in c
