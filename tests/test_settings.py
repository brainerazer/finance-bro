import subprocess
from pathlib import Path


def test_token_env_only(monkeypatch):
    from finance_bro.core import settings as s

    s.get_settings.cache_clear()
    monkeypatch.setenv("MONO_TOKEN", "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost:5432/x")
    cfg = s.get_settings()
    assert cfg.mono_token == "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_token_never_persisted_grep_check():
    # Static guard: no code path writes mono_token to file or DB.
    src = Path("src/finance_bro")
    result = subprocess.run(
        [
            "grep",
            "-rEn",
            r"(open\(.*mono_token|INSERT[^A-Za-z]+.*mono_token|UPDATE[^A-Za-z]+.*mono_token|\.write\(.*mono_token|json\.dump\(.*mono_token)",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    # grep returns 1 when no matches — that's what we want.
    assert result.returncode == 1, f"Token persistence found:\n{result.stdout}"
