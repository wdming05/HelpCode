# HelpCode

终端 AI 编程助手。用自然语言描述任务，Agent 自己读代码、改文件、跑命令。

基于 ReAct 循环驱动 LLM 多步调用工具，支持多模型接入、MCP 工具扩展、子代理与多 Agent 协作，并带一套对齐 SWE-bench 判定协议的评测流水线。

## 特性

| 能力 | 说明 |
|---|---|
| **多协议接入** | OpenAI 兼容协议（qwen / DeepSeek / 百炼等）与 Anthropic 官方协议，配置里切换 |
| **工具调用** | `read_file` / `write_file` / `edit_file` / `bash` / `glob` / `grep`，可按需接 MCP 工具 |
| **两层上下文压缩** | 工具结果落盘 + 结构化摘要；压缩失败有熔断，撞上下文超限会紧急压缩并重试一次 |
| **分层权限** | 四档模式（default / acceptEdits / plan / bypass）+ 规则匹配器 + 路径沙箱 + Hook 拦截 |
| **子代理** | 独立上下文、独立工具集、三层权限链，可后台运行并把结果回注主对话 |
| **Agent Team** | 队员独立进程 + 独立 git worktree，文件级隔离，共享任务表与点对点邮箱 |
| **跨会话记忆** | 会话 JSONL 增量存档、LLM 提炼四类笔记、项目指令文件三层加载 |
| **评测** | 题集加载 → 干净工作区 → rollout → 收补丁 → 打隐藏测试补丁 → 判定 → 报告 |

## 快速开始

需要 Python 3.12+。

```bash
git clone https://github.com/yiiiiim-i/HelpCode.git
cd HelpCode

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

配置模型：

```bash
cp .helpcode/config.yaml.example .helpcode/config.yaml
```

编辑 `.helpcode/config.yaml`，填 `model` 与 `base_url`，密钥用环境变量注入：

```yaml
providers:
  - name: "qwen"
    protocol: openai
    api_key: "${DASHSCOPE_API_KEY}"
    model: "qwen3-max"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
```

```bash
export DASHSCOPE_API_KEY=sk-...    # Windows: set DASHSCOPE_API_KEY=...
helpcode
```

## 常用命令

在会话里输入：

| 命令 | 作用 |
|---|---|
| `/compact` | 手动压缩上下文 |
| `/plan` | 进入计划模式（只读，不改文件） |
| `/session` | 列出 / 切换历史会话 |
| `/memory` | 查看记忆与笔记 |
| `/worktree` | 创建 / 进入 / 退出隔离工作目录 |
| `/team` | 建队、派活、查看花名册 |
| `/status` | 当前模式、模型、上下文占用 |
| `/clear` | 清空当前会话 |

## 架构

按职责分包，依赖单向：

```
cli.py / tui/         交互层（Textual）
      │
agent.py              ReAct 循环：请求 → 工具 → 回灌
      │
├── llm/              协议适配（OpenAI / Anthropic）、错误分类
├── tool/             六个内置工具 + 注册中心 + 路径解析
├── context/          两层压缩：spill（落盘）/ summarize（摘要）
├── prompt/           系统提示装配（稳定段 + 环境段）
├── permission/       模式、规则、匹配器、沙箱
├── hook/             11 个事件、四种动作、exit 2 拦截
├── mcp/              MCP server 连接与工具适配
├── skill/            两阶段加载的技能
├── subagent/         子代理目录、运行器、后台任务
├── team/             多 Agent：任务表、邮箱、锁文件、协调者模式
├── worktree/         git worktree 隔离
├── memory/           会话存档、笔记提炼、指令加载
└── eval/             评测流水线（driver / judge / report）
```

## 开发

```bash
pytest -q                    # 862 个用例
ruff check . && ruff format  # 静态检查与格式化
```

评测子系统自带合成题集，不依赖 Docker：

```bash
python tests/fixtures/build_mini_dataset.py     # 生成 3 道合成题
helpcode eval run --dataset eval_datasets/mini.jsonl --driver local
helpcode eval report runs/<run-id>
```

## 部署（可选）

`deploy/` 里是 Docker + ttyd 的远程访问方案：把终端界面套进浏览器，供自己远程使用。
详见 `deploy/README.md`。

这不是 Web 版——它只是给 TUI 加了一层浏览器外壳，**必须私有部署**（密码 + HTTPS），
因为一旦公开，等于把服务器 shell 交给每一个能打开页面的人。

## 许可

未指定许可证。如需使用请先联系作者。
