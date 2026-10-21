# HelpCode

一个 Claude Code 风格的多协议终端 AI 编程助手（Python）。

> A multi-protocol (Anthropic / OpenAI) terminal AI assistant — Claude Code-style TUI with streaming, multi-turn context, and extended thinking.

## 功能特性

- **双协议后端**：Anthropic / OpenAI，通过 YAML 配置切换（支持自定义 `base_url` 兼容端点）
- **流式输出**：SSE 逐字实时显示，非一次性返回
- **多轮上下文**：单会话内维护完整对话历史
- **扩展思考**：Anthropic 协议下支持 extended thinking（思考增量接收即丢弃，不渲染）
- **完整 TUI**：猫咪 banner、输入框、状态栏、markdown 渲染、响应计时（`Imagining… (Ns)`）
- **多 provider 选择**：多条配置时启动出现方向键列表
- **错误反馈**：请求失败红色样式显示，程序不退出
- **退出**：`/exit` 或 `Ctrl+C`

## 环境要求

- Python >= 3.12
- 现代终端（Windows Terminal 等支持 ConPTY 的终端；旧版 cmd.exe 不支持）

## 安装

```bash
python -m venv .venv
# Windows 激活：.venv\Scripts\activate
pip install -e .
```

## 配置

```bash
cp .helpcode/config.yaml.example .helpcode/config.yaml
```

编辑 `.helpcode/config.yaml`，每个 provider 含 6 个字段：

| 字段 | 说明 |
|------|------|
| `name` | 供应商标识名 |
| `protocol` | `anthropic` 或 `openai` |
| `model` | 模型名 |
| `base_url` | 自定义端点（可空） |
| `api_key` | 密钥 |
| `thinking` | 是否开启扩展思考（仅 anthropic 生效） |

> `.helpcode/config.yaml` 含真实密钥，已被 `.gitignore` 忽略，切勿提交。

## 运行

```bash
python -m helpcode
```

- 输入问题后 `Enter` 发送，`Alt+Enter` 换行
- `/exit` 或 `Ctrl+C` 退出

## 项目结构

```
src/helpcode/
├── cli.py            # 入口装配
├── config.py         # YAML 配置加载与校验
├── prompt.py         # system prompt 与 banner
├── conversation.py   # 多轮对话历史
├── llm/              # Provider 抽象 + anthropic/openai 适配器
└── tui/              # Textual 界面（app/stream/select/view）
```

## 测试

```bash
pytest
ruff check src tests
```
