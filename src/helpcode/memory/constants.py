"""记忆与会话恢复的全部阈值，集中一处。

沿用 ``context/constants.py`` 的理由：这些是算法参数，跟着代码版本走。放进配置
会让「同一份代码跑出不同结果」，而记忆/恢复的行为差异极难复现——它依赖磁盘上
已有多少会话、笔记写了多少条。

集中而非各模块自定义的第二个理由：阈值之间有隐藏关系。
``MAX_INDEX_LINES`` 与 ``MAX_INDEX_BYTES`` 谁先触顶取决于条目平均长度；
``NOTE_CHUNK_MSGS`` 与 ``NOTE_CHUNK_CHARS`` 是一个「双闸」，任一超了都截；
``MAX_INCLUDE_DEPTH`` 必须明显小于用户可能构造的链长，否则「深度保护」形同虚设。
散落各处时，改一个忘一个不会报错——只会在某次真实使用里表现成「笔记丢了」。
"""

from pathlib import Path

# ---------------------------------------------------------------- 项目指令文件

# 单份指令文件的字节上限（防误粘大文件把上下文吃光）
MAX_INSTRUCTION_BYTES = 32 * 1024
# `@` 引用的最大嵌套深度
MAX_INCLUDE_DEPTH = 5
# 判定二进制时只嗅探前这么多字节
# （512 是 file(1) 的经典值：足够覆盖常见魔数，又不至于读整个大文件）
BINARY_SNIFF_BYTES = 512

# ---------------------------------------------------------------- 记忆索引

# 注入的索引最多这么多行（约 2-3K token）
MAX_INDEX_LINES = 200
# 行数没超但字节超了也要截：单行可以很长
MAX_INDEX_BYTES = 25 * 1024

# ---------------------------------------------------------------- 会话存档

# 会话根目录（相对项目根）
SESSIONS_DIR = Path(".helpcode") / "sessions"
# 每个会话目录内的 JSONL 文件名
JSONL_NAME = "conversation.jsonl"
# 每个会话目录内的工具结果落盘目录（F12：与会话绑定，删会话即连带删）
TOOL_RESULTS_NAME = "tool-results"
# 没有会话时的落盘兜底目录（ch08 的全局目录）。
# 值必须与 ``context.constants.SPILL_DIR`` 一致，但**不能** import 它——
# memory 包不依赖 context（N6）。由装配层保证两者指向同一个字符串。
LEGACY_TOOL_RESULTS = Path(".helpcode") / "tool-results"
# 模型标识的最大保留长度：模型名偶尔含很长的版本后缀，落盘前截断防刷屏
MAX_MODEL_LEN = 64

# ---------------------------------------------------------------- 恢复

# 距上次对话超过这么多小时，恢复时插一条时间跨度提醒
TIME_GAP_HOURS = 24
# 提醒消息的前缀。它有两重作用：给模型看「这是提示不是历史」，
# 以及给 writer/restore 一个可过滤的标记（提醒不写进存档，见 D8）
TIME_GAP_MARK = "[时间跨度]"

# ---------------------------------------------------------------- 自动笔记

# 送去提炼的最近消息条数上限
NOTE_CHUNK_MSGS = 6
# 送去提炼的字符数上限（与条数构成双闸，任一超了就截）
NOTE_CHUNK_CHARS = 1500
# 四类笔记。用英文标识而不是中文：它会写进 frontmatter 和文件名，
# 中文在小写化、slug 化时容易出意外（全角、编码差异）
NOTE_TYPES = (
    "user_preference",
    "correction_feedback",
    "project_knowledge",
    "reference_material",
)
# slug 的最大长度（文件名 + 索引行都要短）
NOTE_SLUG_MAX = 40

# ---------------------------------------------------------------- 清理

# 会话保留天数。按会话 ID 里编码的创建时刻计算，不看 mtime（见 D23）
SESSION_TTL_DAYS = 30
