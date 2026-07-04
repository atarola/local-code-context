#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_code_context.mcp.context import (
    get_repository_context,
    get_workspace_context,
    list_indexed_repositories,
    resolve_repo_name,
)
from local_code_context.storage.reader import (
    find_callers,
    find_callees,
    find_calls_by_name,
    get_definition,
    get_file_vibe,
    get_imports,
    list_symbols,
    trace_callers,
    trace_export,
)
from local_code_context.storage.resolver import get_resolved_imports, resolve_imports_for_repo

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "local-code-context"
SERVER_VERSION = "0.4.0"
DEFAULT_DB = "./codebase_index"


@dataclass(frozen=True)
class ServerConfig:
    db: Path


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


def _resolve_repo(config: ServerConfig, repo: str | None) -> str | None:
    if repo is None:
        return None
    resolved = resolve_repo_name(config.db, repo)
    return resolved if resolved else repo


def _call_tool(config: ServerConfig, name: str, arguments: dict[str, Any]) -> str:
    if name == "list_repositories":
        repos = list_indexed_repositories(config.db)
        body = (
            "\n".join(f"- {repo}" for repo in repos)
            or "(no indexed repositories found)"
        )
        return "Indexed repositories:\n" + body
    if name == "get_repository_context":
        repo = arguments.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo is required")
        repo = _resolve_repo(config, repo.strip())
        if repo is None:
            return f"(repository not indexed: {arguments.get('repo')})"
        max_chars = arguments.get("max_chars")
        return get_repository_context(config.db, repo, max_chars=max_chars)
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
                resolved = _resolve_repo(config, repo.strip())
                repo_list.append(resolved if resolved else repo.strip())
        return get_workspace_context(
            config.db, repos=repo_list, max_chars_per_repo=max_chars
        )
    if name == "get_definition":
        symbol = arguments.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        repo = _resolve_repo(config, _argument(arguments, "repo", None))
        path = _argument(arguments, "path", None)
        kind = _argument(arguments, "kind", None)
        limit = arguments.get("limit", 20)
        results = get_definition(
            config.db, name=symbol.strip(),
            repo=repo,
            path=path.strip() if isinstance(path, str) else None,
            kind=kind.strip() if isinstance(kind, str) else None,
            limit=int(limit),
        )
        if not results:
            return f"(no definitions found for symbol: {symbol})"
        lines = []
        for r in results:
            vibe = get_file_vibe(config.db, r["repo"], r["path"])
            line = (
                f"{r['repo']}:{r['path']}:{r['start_line']}-{r['end_line']}"
                f"  kind={r['kind']}"
                f"  parent={r['parent']}" if r['parent'] else ""
            )
            if vibe:
                line += f"  [{vibe}]"
            lines.append(line)
        return "\n".join(lines)
    if name == "get_imports":
        repo = _resolve_repo(config, _argument(arguments, "repo", None))
        path = _argument(arguments, "path", None)
        limit = arguments.get("limit", 100)
        results = get_imports(
            config.db,
            repo=repo,
            path=path.strip() if isinstance(path, str) else None,
            limit=int(limit),
        )
        if not results:
            return "(no imports found)"
        lines = []
        for r in results:
            lines.append(
                f"{r['repo']}:{r['path']}:{r['start_line']}"
                f"  {r['source_module']} -> {r['imported_name']}"
            )
        return "\n".join(lines)
    if name == "trace_export":
        name_arg = arguments.get("name")
        if not isinstance(name_arg, str) or not name_arg.strip():
            raise ValueError("name is required")
        repo = _resolve_repo(config, _argument(arguments, "repo", None))
        result = trace_export(
            config.db, name=name_arg.strip(),
            repo=repo,
        )
        parts = []
        if result.get("definition"):
            parts.append("=== Definition ===")
            for d in result["definition"]:
                parts.append(f"  {d['repo']}:{d['path']}:{d['start_line']}  kind={d['kind']}")
        if result.get("importers"):
            parts.append("=== Imported by ===")
            for i in result["importers"]:
                parts.append(f"  {i['repo']}:{i['path']}")
        if not parts:
            return f"(no trace found for: {name_arg})"
        return "\n".join(parts)
    if name == "trace_callers":
        callee_name = arguments.get("callee")
        if not isinstance(callee_name, str) or not callee_name.strip():
            raise ValueError("callee is required")
        repo = _resolve_repo(config, _argument(arguments, "repo", None))
        results = trace_callers(
            config.db,
            callee_name=callee_name.strip(),
            repo=repo,
        )
        if not results:
            return f"(no callers found for: {callee_name})"
        lines = []
        for r in results:
            lines.append(
                f"{r['repo']}:{r['path']}:{r['start_line']}"
                f"  {r.get('caller_sym_name', '?')} -> {r['callee_name']}"
            )
        return "\n".join(lines)
    if name == "find_callers":
        symbol_id = arguments.get("symbol_id")
        if not isinstance(symbol_id, int):
            raise ValueError("symbol_id is required")
        limit = arguments.get("limit", 100)
        results = find_callers(config.db, symbol_id, limit=int(limit))
        if not results:
            return f"(no callers found for symbol_id: {symbol_id})"
        lines = []
        for r in results:
            lines.append(
                f"{r['repo']}:{r['path']}:{r['start_line']}:{r['start_column']}"
                f"  {r.get('caller_sym_name', '?')} -> {r['callee_name']}"
                f"  status={r['resolution_status']}"
            )
        return "\n".join(lines)
    if name == "find_callees":
        caller_symbol_id = arguments.get("caller_symbol_id")
        if not isinstance(caller_symbol_id, int):
            raise ValueError("caller_symbol_id is required")
        include_unresolved = bool(arguments.get("include_unresolved", True))
        limit = arguments.get("limit", 100)
        results = find_callees(
            config.db, caller_symbol_id,
            include_unresolved=include_unresolved, limit=int(limit),
        )
        if not results:
            return f"(no callees found for caller_symbol_id: {caller_symbol_id})"
        lines = []
        for r in results:
            resolved = (
                f" -> {r['resolved_sym_name']} @ {r['resolved_sym_path']}"
                if r.get("resolved_sym_name") else ""
            )
            lines.append(
                f"{r['repo']}:{r['path']}:{r['start_line']}:{r['start_column']}"
                f"  {r['callee_name']}{resolved}"
            )
        return "\n".join(lines)
    if name == "find_calls_by_name":
        repo = arguments.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo is required")
        resolved_repo = _resolve_repo(config, repo.strip())
        if resolved_repo is None:
            return f"(repository not indexed: {repo})"
        callee_name = arguments.get("callee_name")
        if not isinstance(callee_name, str) or not callee_name.strip():
            raise ValueError("callee_name is required")
        path = _argument(arguments, "path", None)
        limit = arguments.get("limit", 100)
        results = find_calls_by_name(
            config.db, resolved_repo, callee_name.strip(),
            path=path.strip() if isinstance(path, str) else None,
            limit=int(limit),
        )
        if not results:
            return f"(no calls found for {callee_name} in repo: {repo})"
        lines = []
        for r in results:
            resolved = (
                f" -> {r['resolved_sym_name']} ({r['resolved_sym_kind']}) @ {r['resolved_sym_path']}"
                if r.get("resolved_sym_name") else ""
            )
            lines.append(
                f"{r['path']}:{r['start_line']}:{r['start_column']}"
                f"  {r.get('caller_sym_name', '?')} -> {r['callee_name']}{resolved}"
                f"  [{r['resolution_status']}]"
            )
        return "\n".join(lines)
    if name == "list_symbols":
        repo = _resolve_repo(config, _argument(arguments, "repo", None))
        kind = _argument(arguments, "kind", None)
        path = _argument(arguments, "path", None)
        limit = arguments.get("limit", 100)
        results = list_symbols(
            config.db,
            repo=repo,
            kind=kind.strip() if isinstance(kind, str) else None,
            path=path.strip() if isinstance(path, str) else None,
            limit=int(limit),
        )
        if not results:
            return "(no symbols found matching filters)"
        lines = []
        for r in results:
            lines.append(
                f"{r['repo']}:{r['path']}:{r['start_line']}-{r['end_line']}"
                f"  {r['name']}  kind={r['kind']}"
            )
        return "\n".join(lines)
    if name == "resolve_imports":
        repo = arguments.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo is required")
        resolved_repo = _resolve_repo(config, repo.strip())
        if resolved_repo is None:
            return f"(repository not indexed: {repo})"
        rerun = bool(arguments.get("rerun", False))
        if rerun:
            run_result = resolve_imports_for_repo(config.db, resolved_repo)
        results = get_resolved_imports(
            config.db,
            repo=resolved_repo,
            path=_argument(arguments, "path", None),
            limit=int(arguments.get("limit", 100)),
        )
        if not results:
            return f"(no resolved imports for repo: {repo})"
        lines = []
        if rerun:
            lines.append(f"# re-resolution: {run_result['resolved']} resolved, {run_result['unresolved']} unresolved")
            if run_result['errors']:
                lines.append(f"# errors: {len(run_result['errors'])}")
        for r in results:
            lines.append(
                f"{r['repo']}:{r['path']}:{r['source_module']} -> {r['imported_name']}"
                f"  ==> {r['symbol_name']} ({r['symbol_kind']}) @ {r['symbol_path']}:{r['symbol_start_line']}"
            )
        return "\n".join(lines)
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
            "name": "get_definition",
            "description": (
                "Retrieve definitions of a symbol by exact name. "
                "Use repo, path, or kind to narrow. Returns file location, "
                "kind, parent, and file vibe for each match."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Required exact symbol name.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Optional repository filter.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path filter.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Optional symbol kind filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Optional max results. Default: 20.",
                    },
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_imports",
            "description": (
                "List imports for a repo or file. Returns source module, "
                "imported name, and file location for each import."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repository filter.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_export",
            "description": (
                "Find where a symbol is defined and what files import it. "
                "Returns both the definition location(s) and the list of "
                "files that reference it via import."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Required exported name.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Optional repository filter.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_callers",
            "description": (
                "Find all call sites where a given function or method is "
                "called. Returns caller name, callee name, and location "
                "(repo:path:line) for each call site."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "callee": {
                        "type": "string",
                        "description": "Required callee name (function/method being called).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Optional repository filter.",
                    },
                },
                "required": ["callee"],
                "additionalProperties": False,
            },
        },
        {
            "name": "find_callers",
            "description": (
                "Find all call sites that resolve to a given symbol ID. "
                "Returns caller name, callee name, location, and resolution status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol_id": {
                        "type": "integer",
                        "description": "Required symbol ID (use get_definition to find it).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                },
                "required": ["symbol_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "find_callees",
            "description": (
                "List all call sites from a given caller symbol. "
                "Optionally filter to only resolved calls."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "caller_symbol_id": {
                        "type": "integer",
                        "description": "Required caller symbol ID.",
                    },
                    "include_unresolved": {
                        "type": "boolean",
                        "description": "Include unresolved call sites. Default: true.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                },
                "required": ["caller_symbol_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "find_calls_by_name",
            "description": (
                "Find all call sites in a repo matching a callee name, "
                "with resolved symbol details. Optionally filter by path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Required repository name.",
                    },
                    "callee_name": {
                        "type": "string",
                        "description": "Required callee function/method name.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                },
                "required": ["repo", "callee_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_symbols",
            "description": (
                "List all symbols matching filters. Optionally narrow by "
                "repo, kind, or path. Returns name, kind, and location "
                "for each symbol."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional repository filter.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Optional symbol kind filter.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "resolve_imports",
            "description": (
                "Show resolved import chains for a repo. Each import is "
                "mapped to the symbol definition it references. Optionally "
                "re-run resolution and filter by path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Required repository name.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional max results. Default: 100.",
                    },
                    "rerun": {
                        "type": "boolean",
                        "description": "Re-run import resolution before returning results. Default: false.",
                    },
                },
                "required": ["repo"],
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
        description="Expose local-code-context as an MCP server over stdio."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"DB directory. Default: {DEFAULT_DB}"
    )
    args = parser.parse_args()

    config = ServerConfig(
        db=Path(args.db).expanduser().resolve(),
    )

    while True:
        message = _read_message()
        if message is None:
            return
        _handle_message(config, message)


if __name__ == "__main__":
    main()
