"""上下文管理的全部算法参数，集中一处。

为什么不放配置文件：这些是算法参数，跟着代码版本走。放进配置或环境变量会让
「同一份代码跑出不同结果」，A/B 对比就说不清了。

为什么集中而不是各模块自定义：阈值之间有关系（``BATCH_RESULT_MAX`` 必须大于
``TOOL_RESULT_MAX``，``SAFETY_AUTO`` 必须小于保留区 + 余量）。散落各处时，
改一个忘一个不会有任何报错 —— 只会在某个真实会话里表现成「压缩不触发」。
"""

from pathlib import Path

# ---------------------------------------------------------------- 触发线

# 自动触发时预留的安全余量（估算误差的吸收带）
SAFETY_AUTO = 13000
# 手动触发时收窄到 3K：用户主动要压，信任他的判断
SAFETY_MANUAL = 3000
# 窗口至少是安全余量的这么多倍，才允许自动压缩
# （窗口 14000、余量 13000 时触发线只剩 1000，每轮都压比不压更糟）
MIN_WINDOW_RATIO = 1.5

# ---------------------------------------------------------------- 第一层：落盘

# 单条工具结果超过这个 token 数就落盘
TOOL_RESULT_MAX = 8000
# 一次工具调用批的合计上限；超了按单条大小降序依次落盘
BATCH_RESULT_MAX = 16000
# 落盘后保留的预览行数
PREVIEW_LINES = 20
# 预览的字符上限。只按行截会在「单行超长」时失效——minified JSON、压缩过的
# JS、单行 CSV 都只有 1 行，前 20 行就是全文，落盘等于没做（实测 7502 token
# 的原文落盘后得到 7575 token 的「预览」，反而更大）。
PREVIEW_CHARS = 2000
# 豁免落盘的工具。read_file 的结果落盘后，预览里会给出文件路径，模型习惯性
# 用 read_file 读回来 —— 读回来的内容照样超阈值 → 再落盘 → 死循环。
# 豁免后改由 read_file 自己的字符守卫兜底（见 tool/read_file.py）。
SPILL_EXEMPT_TOOLS = frozenset({"read_file"})
# 幂等标记：content 以此开头视为已处理
SPILL_MARK = "[工具结果已落盘]"
# 落盘目录（相对项目根；必须在项目内，否则沙箱不让 read_file 读回）
SPILL_DIR = Path(".helpcode") / "tool-results"

# ---------------------------------------------------------------- 第二层：摘要

# 保留区目标 token 数（从尾部向前累计）
KEEP_TAIL_TOKENS = 10000
# 保留区最少条数（token 目标先到也要凑够这么多条）
KEEP_TAIL_MIN_MSGS = 5
# 待摘要区少于这么多条就不值得摘
MIN_SUMMARIZE_MSGS = 2
# 发摘要请求前，把超长工具结果就地挖空到这个 token 数
TRUNCATE_BEFORE_SUMMARY = 2000
# 摘要请求自身撞上下文超限时，最多丢几次「最老的消息组」
PTL_RETRY_LIMIT = 3
# 摘要正文低于这么多字符视为解析失败
MIN_SUMMARY_CHARS = 50

# ---------------------------------------------------------------- 熔断

# 连续失败这么多次就停止自动压缩（第一层不受影响，它不调 LLM）
SUMMARY_MAX_FAILURES = 3

# ---------------------------------------------------------------- 有效窗口与水位线

# 有效窗口 = 原始窗口 − BUFFER_TOTAL。防撞墙：给大模型输出与摘要请求自身留空间，
# 否则在水位线之上再叠输出 token 会直接 400。
BUFFER_OUTPUT = 13000    # 大模型输出预留（output_tokens + 摘要请求输入）
BUFFER_ESTIMATE = 10000  # 估算误差吸收带（本地估算宁多不少）
BUFFER_TAIL = 10000      # 摘要后保留区兜底（避免压完立刻又压）
BUFFER_TOTAL = BUFFER_OUTPUT + BUFFER_ESTIMATE + BUFFER_TAIL  # 33000

# 渐进水位线（占有效窗口的比例）
MICRO_WATERMARK = 0.55   # L4 局部微压缩触发线
HARD_WATERMARK = 0.80    # L5 全局硬摘要触发线

# L1 落盘比例阈值（占有效窗口比例，替代写死 8000/16000）
SPILL_SINGLE_RATIO = 0.05
SPILL_BATCH_RATIO = 0.10

# L4 局部微压缩：工具链低于这个 token 数不值得摘要
MICRO_CHAIN_MIN = 2000
