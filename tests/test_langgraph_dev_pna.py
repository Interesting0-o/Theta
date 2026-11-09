import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="the LangGraph launcher shim is Windows-only")
def test_langgraph_dev_launcher_allows_private_network_preflight():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    scripts = str(project_root / "scripts")
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    env["THETA_LANGGRAPH_DEV_PNA"] = "1"
    code = """
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware

middleware = CORSMiddleware(
    app=lambda scope, receive, send: None,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
response = middleware.preflight_response(Headers({
    "origin": "https://smith.langchain.com",
    "access-control-request-method": "GET",
    "access-control-request-private-network": "true",
}))
print(response.status_code)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200"
