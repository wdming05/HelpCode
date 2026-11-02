"""权限系统包：六层防御的权限判定引擎（只做 re-export）。"""

from .decision import Decision, Outcome
from .engine import Engine, categorize, check, new_engine
from .modes import Category, Mode, parse_mode
from .os_sandbox import (
    DENY_WRITE_RELATIVE,
    Sandbox,
    SandboxConfig,
    SandboxState,
    create_sandbox,
    default_config,
)
from .pattern import Matcher, PatternError, compile_pattern
from .persist import persist_local_allow
from .rules import Rule, RuleSet, friendly_name
from .sandbox import extract_target

__all__ = [
    "DENY_WRITE_RELATIVE",
    "Category",
    "Decision",
    "Engine",
    "Matcher",
    "Mode",
    "Outcome",
    "PatternError",
    "Rule",
    "RuleSet",
    "Sandbox",
    "SandboxConfig",
    "SandboxState",
    "categorize",
    "check",
    "compile_pattern",
    "create_sandbox",
    "default_config",
    "extract_target",
    "friendly_name",
    "new_engine",
    "parse_mode",
    "persist_local_allow",
]
