"""Static guard — no eval/exec/regex anywhere under categorizer/ (T-4-eval).

Reads every `*.py` under src/finance_bro/categorizer/ and asserts none of the
code-execution / regex tokens appear. This is the enforcement backstop for D-05 /
Anti-pattern 8: the op vocabulary is closed and values are only ever compared,
never executed. Also asserts the package is pure (no SQLAlchemy/session imports).
"""

import re
from pathlib import Path

import finance_bro.categorizer as categorizer_pkg

_FORBIDDEN = [
    re.compile(r"\beval\("),
    re.compile(r"\bexec\("),
    re.compile(r"re\.compile"),
    re.compile(r"^\s*import re\b", re.MULTILINE),
    re.compile(r"^\s*from re\b", re.MULTILINE),
]

_FORBIDDEN_IMPORTS = [
    re.compile(r"import\s+sqlalchemy"),
    re.compile(r"\bAsyncSession\b"),
    re.compile(r"\bsession\b"),
]


def _categorizer_files() -> list[Path]:
    pkg_dir = Path(categorizer_pkg.__file__).parent
    files = sorted(pkg_dir.glob("*.py"))
    assert files, "no python files found under categorizer/"
    return files


def test_no_eval_exec_or_regex_in_categorizer():
    for path in _categorizer_files():
        src = path.read_text()
        for pat in _FORBIDDEN:
            assert not pat.search(src), f"forbidden token {pat.pattern!r} in {path.name}"


def test_categorizer_is_pure_no_db_imports():
    for path in _categorizer_files():
        src = path.read_text()
        for pat in _FORBIDDEN_IMPORTS:
            assert not pat.search(src), f"impure DB token {pat.pattern!r} in {path.name}"
