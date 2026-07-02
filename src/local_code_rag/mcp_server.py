#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_code_rag.query import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_COLLECTION,
    DEFAULT_DB,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
    build_context,
    build_prompt,
    ollama_chat,
    search_chunks,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "local-code-rag"
SERVER_VERSION = "0.1.0"


@dataclass(frozen=True)
class ServerConfig:
    db: Path
    collection: str
    top_k: int
    embed_model: str
    model: str
    ollama_url: str
    repo: str | None


def _send(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if b":" not in line:
            continue
        key, value = line.decode("utf-8", errors="replace").split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = headers.get("content-length")
    if not content_length:
        return None
    try:
        size = int(content_length)
    except ValueError:
        return None

    payload = sys.stdin.buffer.read(size)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _argument(arguments: dict[str, Any], key: str, default: Any) -> Any:
    value = arguments.get(key, default)
    return default if value is None else value


def _query_text(config: ServerConfig, arguments: dict[str, Any]) -> str:
    query = arguments.get("q")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("q is required")

    repo = _argument(arguments, "repo", config.repo)
    top_k = int(_argument(arguments, "top_k", config.top_k))
    embed_model = str(_argument(arguments, "embed_model", config.embed_model))
    model = str(_argument(arguments, "model", config.model))
    ollama_url = str(_argument(arguments, "ollama_url", config.ollama_url))
    collection = str(_argument(arguments, "collection", config.collection))
    db = Path(str(_argument(arguments, "db", str(config.db)))).expanduser().resolve()
    show_context = bool(arguments.get("show_context", False))
    no_answer = bool(arguments.get("no_answer", False))

    hits = search_chunks(
        db_path=db,
        collection_name=collection,
        query=query,
        embed_model=embed_model,
        ollama_url=ollama_url,
        top_k=top_k,
        repo=repo,
    )
    if not hits:
        return "No chunks found."

    lines = []
    for index, hit in enumerate(hits, start=1):
        metadata = hit["metadata"]
        citation = f"{metadata['repo']}:{metadata['path']}:{metadata['start_line']}-{metadata['end_line']}"
        lines.append(f"[{index}] {citation} distance={hit['distance']:.4f}")

    if no_answer:
        return "\n".join(lines)

    prompt = build_prompt(query, hits)
    answer = ollama_chat(prompt, model, ollama_url)
    parts = ["\n".join(lines)]
    if show_context:
        parts.append("--- Retrieved context ---\n" + build_context(hits))
    parts.append("--- Answer ---\n" + answer.strip())
    return "\n\n".join(parts)


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "query_codebase",
            "description": "Search the local Chroma index and optionally ask the local Ollama chat model to answer using the retrieved code.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Question to ask."},
                    "repo": {"type": "string", "description": "Restrict retrieval to a single repo name."},
                    "db": {"type": "string", "description": "Chroma database directory."},
                    "collection": {"type": "string", "description": "Chroma collection name."},
                    "top_k": {"type": "integer", "minimum": 1, "description": "Number of chunks to retrieve."},
                    "embed_model": {"type": "string", "description": "Ollama embedding model."},
                    "model": {"type": "string", "description": "Ollama chat model."},
                    "ollama_url": {"type": "string", "description": "Ollama HTTP API base URL."},
                    "show_context": {"type": "boolean", "description": "Include retrieved chunks in the response."},
                    "no_answer": {"type": "boolean", "description": "Only return retrieval hits."},
                },
                "required": ["q"],
                "additionalProperties": False,
            },
        }
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose local-code-rag as an MCP server over stdio.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help=f"Chroma collection. Default: {DEFAULT_COLLECTION}")
    parser.add_argument("--top-k", type=int, default=10, help="Number of chunks to retrieve. Default: 10")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help=f"Ollama embedding model. Default: {DEFAULT_EMBED_MODEL}")
    parser.add_argument("--model", default=DEFAULT_CHAT_MODEL, help=f"Ollama chat model. Default: {DEFAULT_CHAT_MODEL}")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_URL}")
    parser.add_argument("--repo", help="Default repo name to restrict retrieval to.")
    args = parser.parse_args()

    config = ServerConfig(
        db=Path(args.db).expanduser().resolve(),
        collection=args.collection,
        top_k=args.top_k,
        embed_model=args.embed_model,
        model=args.model,
        ollama_url=args.ollama_url,
        repo=args.repo,
    )

    while True:
        message = _read_message()
        if message is None:
            return

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        try:
            if method == "initialize":
                _send(
                    _result(
                        request_id,
                        {
                            "protocolVersion": PROTOCOL_VERSION,
                            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                            "capabilities": {"tools": {}},
                        },
                    )
                )
            elif method == "initialized":
                continue
            elif method == "tools/list":
                _send(_result(request_id, {"tools": _tools()}))
            elif method == "tools/call":
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name != "query_codebase":
                    raise ValueError(f"unknown tool: {name}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                text = _query_text(config, arguments)
                _send(_result(request_id, _text_result(text)))
            else:
                if request_id is not None:
                    _send(_error(request_id, -32601, f"unknown method: {method}"))
        except Exception as exc:  # pragma: no cover - defensive protocol handling
            if request_id is not None:
                _send(_error(request_id, -32000, str(exc)))


if __name__ == "__main__":
    main()
