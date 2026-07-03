#!/usr/bin/env python3
from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any


DEFAULT_DB = "./codebase_index"
DEFAULT_COLLECTION = "code_chunks"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_CHAT_MODEL = "qwen2.5-coder:14b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def ollama_embed(text: str, model: str, base_url: str) -> list[float]:
    import requests

    response = requests.post(
        f"{base_url.rstrip('/')}/api/embed",
        json={"model": model, "input": text},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
        return embeddings[0]
    raise RuntimeError(f"unexpected Ollama embed response: {payload}")


def search_chunks(
    db_path: Path,
    collection_name: str,
    query: str,
    embed_model: str,
    ollama_url: str,
    top_k: int,
    repo: str | None,
) -> list[dict[str, Any]]:
    import chromadb

    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.get_or_create_collection(collection_name)
    query_embedding = ollama_embed(query, embed_model, ollama_url)
    where = {"repo": repo} if repo else None
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    hits: list[dict[str, Any]] = []
    for doc, metadata, distance in zip(
        results.get("documents", [[]])[0],
        results.get("metadatas", [[]])[0],
        results.get("distances", [[]])[0],
        strict=True,
    ):
        hits.append({"document": doc, "metadata": metadata, "distance": distance})
    return hits


def format_citation(metadata: dict[str, Any]) -> str:
    return f"{metadata['repo']}:{metadata['path']}:{metadata['start_line']}-{metadata['end_line']}"


def build_context(hits: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for i, hit in enumerate(hits, start=1):
        citation = format_citation(hit["metadata"])
        blocks.append(f"[{i}] {citation}\n```text\n{hit['document']}\n```")
    return "\n\n".join(blocks)


def build_prompt(question: str, hits: list[dict[str, Any]]) -> str:
    context = build_context(hits)
    return textwrap.dedent(
        f"""
        You are answering a codebase question using retrieved source chunks.

        Rules:
        - Ground your answer in the provided context.
        - Cite files using the bracket numbers and repo:path:line-range labels.
        - If the context is insufficient, say what is missing and what to inspect next.
        - Be direct and focus on code behavior.

        Question:
        {question}

        Retrieved context:
        {context}
        """
    ).strip()


def ollama_chat(prompt: str, model: str, base_url: str) -> str:
    import requests

    response = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise coding assistant. Use citations from retrieved code context.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=300,
    )
    response.raise_for_status()
    payload = response.json()
    message = payload.get("message", {})
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"unexpected Ollama chat response: {payload}")
    return content


def print_hits(hits: list[dict[str, Any]]) -> None:
    for i, hit in enumerate(hits, start=1):
        citation = format_citation(hit["metadata"])
        print(f"[{i}] {citation} distance={hit['distance']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query a local multi-repo Chroma code index with Ollama."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}"
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Chroma collection. Default: {DEFAULT_COLLECTION}",
    )
    parser.add_argument("--q", required=True, help="Question to ask.")
    parser.add_argument("--repo", help="Restrict retrieval to a single repo name.")
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
        "--show-context",
        action="store_true",
        help="Print retrieved chunks before the answer.",
    )
    parser.add_argument(
        "--no-answer",
        action="store_true",
        help="Only print retrieval hits; do not call the chat model.",
    )
    args = parser.parse_args()

    hits = search_chunks(
        db_path=Path(args.db).expanduser().resolve(),
        collection_name=args.collection,
        query=args.q,
        embed_model=args.embed_model,
        ollama_url=args.ollama_url,
        top_k=args.top_k,
        repo=args.repo,
    )

    if not hits:
        print("No chunks found.")
        return

    print_hits(hits)
    if args.no_answer:
        return

    if args.show_context:
        print("\n--- Retrieved context ---\n")
        print(build_context(hits))

    prompt = build_prompt(args.q, hits)
    answer = ollama_chat(prompt, args.model, args.ollama_url)
    print("\n--- Answer ---\n")
    print(answer.strip())


if __name__ == "__main__":
    main()
