from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


class MCPTransportTests(unittest.TestCase):
    def test_newline_delimited_initialize_and_tools_list(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_ROOT)

        with tempfile.TemporaryDirectory() as tmpdir:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "local_code_rag.mcp.server",
                    "--db",
                    tmpdir,
                ],
                cwd=str(REPO_ROOT),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
                proc.stdin.write(json.dumps(initialize) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline().strip()
                response = json.loads(line)
                self.assertEqual(response["jsonrpc"], "2.0")
                self.assertEqual(response["id"], 1)
                self.assertEqual(
                    response["result"]["serverInfo"]["name"], "local-code-rag"
                )

                tools_list = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
                proc.stdin.write(json.dumps(tools_list) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline().strip()
                response = json.loads(line)
                tool_names = {tool["name"] for tool in response["result"]["tools"]}
                self.assertIn("list_repositories", tool_names)
                self.assertIn("get_repository_context", tool_names)
                self.assertIn("get_workspace_context", tool_names)
                self.assertIn("search_code", tool_names)
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
