"""slug 安全校验：能挡住什么、放过什么。"""

import pytest

from helpcode.worktree.slug import (
    MAX_LEN,
    SlugError,
    branch_for,
    flat_slug,
    validate_slug,
)


@pytest.mark.parametrize(
    "name",
    ["fix-login", "feat/a", "a.b_c-1", "A1", "x" * MAX_LEN, "feat/a/b"],
)
def test_valid_slugs(name):
    assert validate_slug(name) == name


def test_empty_rejected():
    with pytest.raises(SlugError, match="不能为空"):
        validate_slug("   ")


def test_too_long_rejected():
    with pytest.raises(SlugError, match="长度"):
        validate_slug("x" * (MAX_LEN + 1))


@pytest.mark.parametrize("name", ["..", "../x", "x/../y", "."])
def test_dot_segments_rejected(name):
    with pytest.raises(SlugError):
        validate_slug(name)


@pytest.mark.parametrize("name", ["a b", "a:b", "a\\b", "a*b", "中文", "a;b"])
def test_illegal_chars_rejected(name):
    with pytest.raises(SlugError, match="非法字符|空段"):
        validate_slug(name)


@pytest.mark.parametrize("name", ["/a", "a/", "a//b"])
def test_empty_segments_rejected(name):
    with pytest.raises(SlugError, match="空段"):
        validate_slug(name)


def test_flat_and_branch():
    assert flat_slug("feat/login") == "feat+login"
    assert branch_for("feat/login") == "worktree-feat+login"
    assert branch_for("fix") == "worktree-fix"
