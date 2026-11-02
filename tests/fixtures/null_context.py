"""永不压缩的 ContextManager 桩。

给 ch04–ch07 的既有用例复用：它们只关心 Loop / 权限 / TUI 的行为，
压缩路径在 test_context_*.py 里单独覆盖。
"""

from helpcode.context import CompressReport


class NullContext:
    context_window = 128000

    def estimate(self, conv):
        return 0

    def should_compress(self, conv):
        return False

    def after_stream(self, usage, message_count):
        return None

    def has_room_after_compress(self, tokens_after):
        return True

    async def before_request(self, conv, cancel_event, tool_names):
        return CompressReport()

    async def compact_now(self, conv, emergency=False, tool_names=None):
        return CompressReport(notice="无需压缩")
