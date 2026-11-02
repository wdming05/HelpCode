"""权限系统包：五层防御的权限判定引擎（只做 re-export）。"""

from .decision import Decision, Outcome
from .engine import Engine, categorize, check, new_engine
from .modes import Category, Mode, parse_mode
from .pattern import Matcher, PatternError, compile_pattern
from .persist import persist_local_allow
from .rules import Rule, RuleSet, friendly_name
from .sandbox import extract_target

__all__ = [
    "Category",
    "Decision",
    "Engine",
    "Matcher",
    "Mode",
    "Outcome",
    "PatternError",
    "Rule",
    "RuleSet",
    "categorize",
    "check",
    "compile_pattern",
    "extract_target",
    "friendly_name",
    "new_engine",
    "parse_mode",
    "persist_local_allow",
]
