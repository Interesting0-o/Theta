"""app/resource —— **资源层**：agent 之外的东西（路径 / 记忆文件 / 技能目录 / 图片文件 / 画像文件），
读进来、转成内部表示。判据（docs/ARCHITECTURE.md §4 的 A/B/C）三条都不中 → 不属于 `app/agent/`，
落在这一层。

```
app/resource/
  paths.py    落盘路径单点（workspace_key / workspace_dir / session_db_path / memory_root /
              sessions_dir / iter_session_dbs / remove_legacy_single_db）
  memory.py   认知资源：长期记忆 md 的读写单点（解析 / 追加 / 覆写 / 注入块）
  skills.py   能力资源：技能库的扫描 / 解析 / 渲染只读单点
  images.py   输入资源：@路径 → 请求体副本的调用期转码（不碰 state，也不做沙箱拒绝）
  profile.py  认知资源：工作区根 AGENT.md（项目画像）的读取与注入块
```

**分层不变量**：本包只依赖 stdlib / `langchain_core` 的消息类型 + `app.schema`，**不 import
app.agent**——后者的 `__init__` re-export graph → nodes，而 nodes 顶部读 `get_settings()`
（import 即要 .env），故 `app.platform` 与测试能顶层 import 本包。反向则完全正常：
`app.agent.*` / `app.platform.*` 都可以 import 本包。

`__init__` 只转出**路径函数**（`paths.py` 那一族——三方消费者共用，写得最多）；其余各支按
`from app.resource.memory import memory_block` 这种显式子模块导入取用。`RESOURCE_ROOT` 常量
**刻意不转出**（monkeypatch 会被静默吞掉，理由见 `paths.py` 的 docstring）。
"""
from app.resource.paths import (
    iter_session_dbs,
    memory_root,
    remove_legacy_single_db,
    session_db_path,
    sessions_dir,
    workspace_dir,
    workspace_key,
)

__all__ = [
    "iter_session_dbs",
    "memory_root",
    "remove_legacy_single_db",
    "session_db_path",
    "sessions_dir",
    "workspace_dir",
    "workspace_key",
]
