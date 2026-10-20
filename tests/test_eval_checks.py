"""evaluation/checks.py 新增的轨迹/文件断言单测。

这些断言是"评估的评分器"：写错会让评估误报。尤其轨迹类断言读的是
`EvalResult.trace`（runner 采集的结构化事件），不是解析模型正文。
"""
from types import SimpleNamespace

from evaluation.checks import (
    failed_tool_seen,
    file_contains,
    file_matches,
    files_exist,
    pytest_passes,
    tool_call_count_between,
    tool_called,
)


def _result(trace=None):
    return SimpleNamespace(trace=trace or [])


def _call(name):
    return {
        "kind": "ai_tool_calls",
        "tool_calls": [{"name": name, "args": {}, "id": f"call-{name}"}],
    }


def _tool(name, status):
    return {"kind": "tool_result", "name": name, "status": status}


def test_file_contains_requires_all_needles(tmp_path):
    target = tmp_path / "summary.md"
    target.write_text("总行数 1200\nERROR 30\nFATAL 0\n", encoding="utf-8")

    passed, _ = file_contains("summary.md", "1200", "ERROR").fn(_result(), tmp_path)
    assert passed is True

    passed, detail = file_contains("summary.md", "1200", "999").fn(_result(), tmp_path)
    assert passed is False
    assert "999" in detail


def test_file_matches_uses_regex(tmp_path):
    target = tmp_path / "summary.md"
    target.write_text("ERROR 30\nFATAL 0\n", encoding="utf-8")

    assert file_matches("summary.md", r"ERROR\s+30").fn(_result(), tmp_path)[0] is True
    assert file_matches("summary.md", r"FATAL\s+9").fn(_result(), tmp_path)[0] is False


def test_tool_called_counts_across_events():
    trace = [_call("run_command"), _call("run_command"), _call("read_file")]

    assert tool_called("run_command", min_times=2).fn(_result(trace), None)[0] is True
    assert tool_called("run_command", min_times=3).fn(_result(trace), None)[0] is False
    assert tool_called("write_file").fn(_result(trace), None)[0] is False


def test_tool_call_count_between():
    trace = [_call("run_command")] * 4

    assert tool_call_count_between("run_command", 2, 5).fn(_result(trace), None)[0] is True
    assert tool_call_count_between("run_command", 5, 9).fn(_result(trace), None)[0] is False


def test_failed_tool_seen_uses_structured_status():
    trace = [_tool("run_command", "failed"), _tool("read_file", "ok")]

    assert failed_tool_seen().fn(_result(trace), None)[0] is True
    assert failed_tool_seen("run_command").fn(_result(trace), None)[0] is True
    assert failed_tool_seen("read_file").fn(_result(trace), None)[0] is False
    assert failed_tool_seen().fn(_result([_tool("read_file", "ok")]), None)[0] is False


def test_failed_tool_seen_counts_denials():
    trace = [_tool("run_command", "denied")]
    assert failed_tool_seen("run_command").fn(_result(trace), None)[0] is True


def test_files_exist(tmp_path):
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("", encoding="utf-8")

    assert files_exist("app.py", "templates/index.html").fn(_result(), tmp_path)[0] is True
    passed, detail = files_exist("app.py", "missing.py").fn(_result(), tmp_path)
    assert passed is False
    assert "missing.py" in detail


def test_pytest_passes_runs_the_workspace_suite(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    passed, detail = pytest_passes("tests").fn(_result(), tmp_path)
    assert passed is True, detail


def test_pytest_passes_flags_failures(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")

    passed, detail = pytest_passes("tests").fn(_result(), tmp_path)
    assert passed is False
    assert "退出码" in detail
