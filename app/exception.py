class AgentError(Exception):
    """CodingAgent 领域异常基类（全项目共享的语言层）。

    作为 guard 分类的边界：`AgentError` 及其子类 = 可预期的业务失败，
    会转成带 `error_type` 的 `ToolResult` 返回给大模型；其余异常 = 内部 bug。
    """


class ConfigError(AgentError):
    """配置缺失或非法。启动即停，引导用户修改。"""


class WorkspaceViolationError(AgentError):
    """路径超出工作区沙箱边界。安全事件，必须显式处理。

    携带结构化字段 `path` / `workspace`，让调用方（guard / 日志）既能读正文，
    也能拿到可编程的路径信息；`str()` 仍给出可读的中文说明。
    """

    def __init__(self, path: str, workspace: str):
        self.path = str(path)
        self.workspace = str(workspace)
        super().__init__(f"路径 {self.path} 不在工作区{self.workspace}内")