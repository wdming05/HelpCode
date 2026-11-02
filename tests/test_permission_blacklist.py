"""黑名单测试。覆盖 AC1/F1/N1。"""

from helpcode.permission.blacklist import matches


def test_rm_root():
    assert matches("rm -rf /") is not None


def test_rm_sensitive_prefix():
    assert matches("rm -rf /home") is not None
    assert matches("rm -rf /etc") is not None
    assert matches("rm -rf /usr") is not None
    assert matches("rm -rf /home/user/proj") is not None


def test_rm_variant():
    assert matches("rm -fr /") is not None
    assert matches("rm -rf --no-preserve-root /") is not None


def test_benign_not_matched():
    assert matches("echo hi") is None
    assert matches("git status") is None
    assert matches("rm notes.txt") is None  # 无 flag 且非敏感路径


def test_disk_device():
    assert matches("dd if=/dev/zero of=/dev/sda") is not None
    assert matches("echo x > /dev/sda") is not None


def test_fork_bomb_and_mkfs():
    assert matches(":(){ :|:& };:") is not None
    assert matches("mkfs.ext4 /dev/sda1") is not None
