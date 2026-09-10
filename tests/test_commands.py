"""app/platform/commands/ 的单测：命令解析（提示词型 / 基座动作型 / 普通文本 / 未识别）。

命令是基座的控制面事件（图之外），解析不依赖 app.agent → 本文件无需 .env。
"""
from app.platform.commands import (
    ACTION_COMMANDS,
    PROMPT_COMMANDS,
    UNKNOWN_COMMAND_HINT,
    ActionCommand,
    PromptCommand,
    help_text,
    is_command,
    parse_command,
)


def test_init_parses_to_prompt_command():
    cmd = parse_command("/init")

    assert isinstance(cmd, PromptCommand)
    assert cmd.name == "/init"
    # 载荷就是命令表里那段预设提示词（真正要投给模型的东西）
    assert cmd.prompt == PROMPT_COMMANDS["/init"]["prompt"]
    assert len(cmd.prompt) > 200  # 是一段作业指导，不是占位符
    assert "AGENT.md" in cmd.prompt  # 明确要求写哪个文件
    # 是"生成或更新"（新建/写入两条路都有交代），不是只说新建
    assert "已存在" in cmd.prompt and "create_file" in cmd.prompt and "edit_file" in cmd.prompt


def test_action_command_slot_parses(monkeypatch):
    """基座动作型：表里加项即被 parse 成 ActionCommand（handler 收 platform + args）。"""

    async def handler(platform, args):  # pragma: no cover —— 只验解析，不执行
        ...

    monkeypatch.setitem(ACTION_COMMANDS, "/fake", {"desc": "测试用", "handler": handler})

    cmd = parse_command("/fake")

    assert isinstance(cmd, ActionCommand)
    assert cmd.name == "/fake" and cmd.handler is handler and cmd.args == ""


def test_help_is_action_command():
    cmd = parse_command("/help")

    assert isinstance(cmd, ActionCommand)
    assert cmd.name == "/help"


def test_session_commands_parse_with_multiword_names_and_args():
    """名字可含空格（`/list session`），解析取最长匹配、余下部分作 args。"""
    listing = parse_command("/list session")
    assert isinstance(listing, ActionCommand)
    assert listing.name == "/list session" and listing.args == ""

    fresh = parse_command("/new session")
    assert fresh is not None and fresh.name == "/new session" and fresh.args == ""

    switching = parse_command("/session   aaaa1111")  # 多余空白折叠
    assert isinstance(switching, ActionCommand)
    assert switching.name == "/session" and switching.args == "aaaa1111"

    # 无参数也命中（handler 会给用法提示），不是"未识别"
    bare = parse_command("/session")
    assert bare is not None and bare.args == ""

    # 多给一段也归到 args（不新增命令）
    extra = parse_command("/new session now")
    assert extra is not None and extra.name == "/new session" and extra.args == "now"

    # "/list" 本身不是命令（表里只有 "/list session"）→ 交给"未识别"提示
    assert parse_command("/list") is None
    assert is_command("/list")


def test_help_text_lists_every_command_with_desc():
    """帮助由两张表生成：表里每条命令都出现，且带自己的 desc（加命令不用改文案）。"""
    text = help_text()

    for name, entry in (*PROMPT_COMMANDS.items(), *ACTION_COMMANDS.items()):
        assert name in text
        assert entry["desc"] in text
    assert "可用命令" in text
    # desc 不再自带命令名（对齐交给 help_text 的格式串）——否则会印成 "  /help /help 显示…"
    assert not PROMPT_COMMANDS["/init"]["desc"].startswith("/init")


def test_unknown_command_hint_reuses_help_text():
    assert "/init" in UNKNOWN_COMMAND_HINT and "/help" in UNKNOWN_COMMAND_HINT
    assert UNKNOWN_COMMAND_HINT.endswith(help_text())


def test_plain_text_is_not_a_command():
    for text in ("帮我看看这个函数", "", "   ", "/", "init", "  /init-x "):
        assert parse_command(text) is None


def test_whitespace_around_command_is_tolerated():
    cmd = parse_command("  /init  ")

    assert cmd is not None and cmd.name == "/init"


def test_is_command_only_for_slash_prefix():
    assert is_command("/nope")
    assert is_command("  /init ")
    assert not is_command("帮我看看")
    assert not is_command("")
