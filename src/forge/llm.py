"""Ollama client: chat w/ tool calls (Qwen coder) + embeddings (EmbeddingGemma)."""

import json
import re
import threading
from typing import Any

import httpx
import numpy as np

from . import config

_sem = threading.BoundedSemaphore(config.LLM_CONCURRENCY)
_client = httpx.Client(timeout=600)


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    seed: int | None = None,
    fmt: str | dict[str, Any] | None = None,
    num_predict: int = 1024,
) -> tuple[dict[str, Any], dict[str, int]]:
    body: dict[str, Any] = {
        "model": config.CODER_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": config.NUM_CTX, "num_predict": num_predict},
    }
    if seed is not None:
        body["options"]["seed"] = seed
    if tools:
        body["tools"] = tools
    if fmt:
        body["format"] = fmt
    with _sem:
        r = _client.post(f"{config.OLLAMA_URL}/api/chat", json=body)
    r.raise_for_status()
    data = r.json()
    msg = data["message"]
    msg.setdefault("content", "")
    if tools and not msg.get("tool_calls"):
        hit = parse_inline_tool_call(msg["content"])
        if hit:
            msg["tool_calls"] = [hit[0]]
            msg["content"] = msg["content"][: hit[1]]  # drop everything the model imagined after its first call
            msg["inline"] = True
    return msg, {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)}


_decoder = json.JSONDecoder()


def parse_inline_tool_call(text: str) -> tuple[dict[str, Any], int] | None:
    """Small models often emit tool calls as JSON in the content (often several, plus imagined results).
    Take the FIRST {"name": ..., "arguments": ...} object; return (call, end_offset)."""
    for m in re.finditer(r"\{", text):
        try:
            obj, end = _decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            return {"function": {"name": obj["name"], "arguments": args}}, end
    return None


def json_call(prompt: str, system: str = "Reply with JSON only.", temperature: float = 0.0) -> Any:
    msg, _ = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=temperature,
        fmt="json",
        num_predict=600,
    )
    try:
        return json.loads(msg["content"])
    except Exception:
        return {"_raw": msg["content"]}


def embed(texts: list[str], kind: str = "doc", batch: int = 32) -> np.ndarray:
    """EmbeddingGemma uses task prefixes; returns L2-normalized float32 matrix."""
    pre = "task: code retrieval | query: " if kind == "query" else "title: none | text: "
    out = []
    for i in range(0, len(texts), batch):
        chunk = [pre + t[:6000] for t in texts[i : i + batch]]
        r = _client.post(f"{config.OLLAMA_URL}/api/embed", json={"model": config.EMBED_MODEL, "input": chunk})
        r.raise_for_status()
        out.extend(r.json()["embeddings"])
    m = np.asarray(out, dtype=np.float32)
    if len(m):
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    return m


def ping() -> list[str] | None:
    try:
        tags = _client.get(f"{config.OLLAMA_URL}/api/tags", timeout=3).json()
        return [m["name"] for m in tags.get("models", [])]
    except Exception:
        return None
