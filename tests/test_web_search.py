"""web_search 上游返回翻译的测试：_translate_upstream + extract/crawl/research 三个特制渲染。

翻译逻辑已随单一职责移到 mcp_service/web_search.py（只被该 server 使用）；这里直接
import 它。web_search.py 在 import 期检查 TAVILY_API_KEY，故先用 setdefault 塞一个
测试占位键（不发起真实网络请求），因此**不要求 .env / WORKSPACE_PATH**，可独立运行：

    uv run python -m pytest tests/test_web_search.py

覆盖要点：
- 三个 wrapper 把上游 dict 渲染成字符串正文（ToolResult.content 恒为 str，
  不违反 agent_schema 的 schema 约束）；
- 失败形态分类：{"error": ...} → upstream_error、空结果字符串 → no_results、
  无法识别的类型 → upstream_error；
- 特制渲染保留交付物（正文/URL/状态），并显式列出失败项。
"""
import os

os.environ.setdefault("TAVILY_API_KEY", "test-key")

from mcp_service.web_search import (
    _crawl_result_to_text,
    _extract_result_to_text,
    _render_crawl,
    _render_extract,
    _render_research,
    _research_result_to_text,
)


# ---------------- 成功形态渲染 ----------------

def test_extract_renders_url_and_raw_content():
    raw = {"results": [{"url": "https://a.com", "raw_content": "正文A"}], "failed_results": []}
    out = _extract_result_to_text(raw)
    assert out.success is True
    assert out.error_type is None
    assert "https://a.com" in out.content
    assert "正文A" in out.content


def test_extract_lists_failed_urls():
    raw = {
        "results": [{"url": "https://a.com", "raw_content": "正文"}],
        "failed_results": ["https://b.com"],
    }
    out = _extract_result_to_text(raw)
    assert "提取失败的 URL: https://b.com" in out.content


def test_crawl_renders_title_url_content():
    raw = {"results": [{"url": "https://a.com", "title": "标题", "content": "页面内容"}]}
    out = _crawl_result_to_text(raw)
    assert "标题" in out.content
    assert "https://a.com" in out.content
    assert "页面内容" in out.content


def test_research_receipt_renders_status_and_id():
    raw = {"request_id": "r1", "status": "in_progress", "input": "课题", "model": "auto"}
    out = _research_result_to_text(raw)
    assert "r1" in out.content
    assert "in_progress" in out.content
    assert "课题" in out.content


def test_research_renders_content_and_sources():
    raw = {
        "request_id": "r2",
        "status": "completed",
        "content": "报告正文",
        "sources": [{"title": "S", "url": "https://s.com"}],
    }
    out = _research_result_to_text(raw)
    assert "报告正文" in out.content
    assert "https://s.com" in out.content


# ---------------- 失败形态分类 ----------------

def test_upstream_error_dict_becomes_upstream_error():
    out = _extract_result_to_text({"error": "invalid key"})
    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "invalid key" in out.content


def test_no_results_str_becomes_no_results():
    out = _crawl_result_to_text("No crawl results found... Suggestions: ...")
    assert out.success is False
    assert out.error_type == "no_results"


def test_unrecognized_type_becomes_upstream_error():
    out = _research_result_to_text(12345)
    assert out.success is False
    assert out.error_type == "upstream_error"


# ---------------- schema 硬约束：content 恒为 str ----------------

def test_all_wrappers_always_return_str_content():
    """任何上游形态下 content 都必须是 str，dict 绝不允许漏出（schema 约束）。"""
    wrappers = (_extract_result_to_text, _crawl_result_to_text, _research_result_to_text)
    samples = (
        {"results": [{"url": "u", "raw_content": "c"}]},
        {"error": "x"},
        "空结果提示串",
        None,
        123,
    )
    for wrapper in wrappers:
        for raw in samples:
            out = wrapper(raw)
            assert isinstance(out.content, str), f"{wrapper.__name__}({raw!r})"
            assert isinstance(out.success, bool)


def test_empty_results_render_placeholder():
    assert "未从指定 URL 提取到内容" in _render_extract({"results": []})
    assert "未爬取到任何页面内容" in _render_crawl({"results": []})
    assert "未知" in _render_research({})
