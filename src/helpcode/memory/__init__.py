"""记忆与会话恢复。

三个子系统，各自独立可测：

- ``instructions``：三层项目指令文件加载 + ``@`` 引用展开
- ``session`` / ``restore`` / ``cleanup``：会话 JSONL 存档、回放修复、过期清理
- ``notes``：自动笔记提炼与索引维护

依赖方向是单向的：本包**不** import ``agent`` / ``tui`` / ``permission`` /
``mcp`` / ``context``。需要压缩时由装配层调用，本包只交出消息列表。
"""
