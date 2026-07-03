#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_code_rag.mcp_context import (
    get_repository_context,
    get_workspace_context,
    list_indexed_repositories,
    search_code,
)
from local_code_rag.query import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_COLLECTION,
    DEFAULT_DB,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "local-code-rag"
SERVER_VERSION = "0.3.0"


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
    payload = json.dumps(message, separators=(",", ":"))
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def _read_message() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"Invalid MCP message: {exc}", file=sys.stderr)
            continue
        if not isinstance(message, dict):
            print("Ignoring non-object MCP message", file=sys.stderr)
            continue
        return message


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


def _call_search(
    config: ServerConfig, arguments: dict[str, Any], *, default_repo: str | None
) -> str:
    query = arguments.get("q")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("q is required")

    repo = _argument(arguments, "repo", default_repo)
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise ValueError("repo must be a non-empty string when provided")

    top_k = arguments.get("top_k")
    if top_k is not None:
        top_k = int(top_k)

    show_context = bool(arguments.get("show_context", True))

    return search_code(
        config,
        q=query,
        repo=repo.strip() if isinstance(repo, str) else None,
        top_k=top_k,
        show_context=show_context,
    )


def _call_tool(config: ServerConfig, name: str, arguments: dict[str, Any]) -> str:
    if name == "list_repositories":
        repos = list_indexed_repositories(config)
        body = (
            "\n".join(f"- {repo}" for repo in repos)
            or "(no indexed repositories found)"
        )
        return "Indexed repositories:\n" + body
    if name == "get_repository_context":
        repo = arguments.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo is required")
        max_chars = arguments.get("max_chars")
        return get_repository_context(config, repo.strip(), max_chars=max_chars)
    if name == "get_workspace_context":
        repos = arguments.get("repos")
        if repos is not None and not isinstance(repos, list):
            raise ValueError("repos must be an array of repository names")
        max_chars = arguments.get("max_chars_per_repo")
        repo_list = None
        if isinstance(repos, list):
            repo_list = []
            for repo in repos:
                if not isinstance(repo, str) or not repo.strip():
                    raise ValueError("repos entries must be non-empty strings")
                repo_list.append(repo.strip())
        return get_workspace_context(
            config, repos=repo_list, max_chars_per_repo=max_chars
        )
    if name == "search_code":
        return _call_search(config, arguments, default_repo=None)
    if name == "query_codebase":
        return _call_search(config, arguments, default_repo=config.repo)
    raise ValueError(f"unknown tool: {name}")


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_repositories",
            "description": (
                "Required args: none. Returns every indexed repository name as raw "
                "context. The model should analyze the list itself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "get_repository_context",
            "description": (
                "Required args: repo. Optional args: max_chars. Returns a deterministic "
                "repository context packet with file tree, README, manifests, entry points, "
                "modules, configuration, persistence, tests, and excerpts. Analyze the "
                "returned context yourself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Required repository name.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional total output limit. Default is 30000.",
                    },
                },
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_workspace_context",
            "description": (
                "Required args: none. Optional args: repos, max_chars_per_repo. Returns a "
                "compact context packet for each repository. Omit repos to include every "
                "indexed repository. Analyze the returned context yourself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional repository name list. Omit this field to include every "
                            "indexed repository."
                        ),
                    },
                    "max_chars_per_repo": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional per-repository output limit. Default is 8000.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "search_code",
            "description": (
                "Required args: q. Optional args: repo, top_k, show_context. Searches the "
                "index and returns citations plus raw retrieved chunks. Omit repo to search "
                "all indexed repositories. The model should analyze the retrieved context "
                "itself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Required search query.",
                    },
                    "repo": {
                        "type": "string",
                        "description": (
                            "Optional repository restriction. Omit this field to search all "
                            "indexed repositories."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Optional result count.",
                    },
                    "show_context": {
                        "type": "boolean",
                        "description": "Optional boolean, default true.",
                    },
                },
                "required": ["q"],
                "additionalProperties": False,
            },
        },
        {
            "name": "query_codebase",
            "description": (
                "Deprecated alias for search_code. Required args: q. Optional args: repo, "
                "top_k, show_context. Omit repo to search all indexed repositories. Returns "
                "raw retrieval context only; the model should analyze it itself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Required search query."},
                    "repo": {
                        "type": "string",
                        "description": "Optional repository restriction. Omit to search all indexed repositories.",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Optional result count.",
                    },
                    "show_context": {
                        "type": "boolean",
                        "description": "Optional boolean, default true.",
                    },
                },
                "required": ["q"],
                "additionalProperties": False,
            },
        },
    ]


def _handle_message(config: ServerConfig, message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            requested_version = None
            if isinstance(params, dict):
                requested_version = params.get("protocolVersion")

            protocol_version = (
                requested_version
                if isinstance(requested_version, str)
                else PROTOCOL_VERSION
            )
            _send(
                _result(
                    request_id,
                    {
                        "protocolVersion": protocol_version,
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                        "capabilities": {"tools": {}},
                    },
                )
            )
            return

        if method == "notifications/initialized":
            return

        if method == "ping":
            _send(_result(request_id, {}))
            return

        if method == "tools/list":
            _send(_result(request_id, {"tools": _tools()}))
            return

        if method == "tools/call":
            if not isinstance(params, dict):
                raise ValueError("params must be an object")

            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not name:
                raise ValueError("name is required")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")

            text = _call_tool(config, name, arguments)
            _send(_result(request_id, _text_result(text)))
            return

        if request_id is not None:
            _send(_error(request_id, -32601, f"unknown method: {method}"))

    except Exception as exc:
        print(f"Error handling MCP method {method!r}: {exc}", file=sys.stderr)
        if request_id is not None:
            _send(_error(request_id, -32000, str(exc)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expose local-code-rag as an MCP server over stdio."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}"
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Chroma collection. Default: {DEFAULT_COLLECTION}",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of chunks to retrieve. Default: 10",
    )
    parser.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Ollama embedding model. Default: {DEFAULT_EMBED_MODEL}",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_CHAT_MODEL,
        help=f"Ollama chat model. Default: {DEFAULT_CHAT_MODEL}",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_URL}",
    )
    parser.add_argument(
        "--repo",
        help="Optional default repository restriction for the deprecated query_codebase alias.",
    )
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
        _handle_message(config, message)


if __name__ == "__main__":
    main()
