"""D4-A P1: secret files are critical for update-conflict classification.

``ConflictAnalyzer.CRITICAL_FILES`` gains exactly three secret basenames —
``.env``, ``.env.local``, ``.env.local.backup`` — so any incoming or local
change to them forces the operator approval path (``services/git/service.py``
consumers). Covered proofs:

1. the three basenames are classified as critical (plain, nested, and
   directory-qualified paths);
2. unrelated filenames are NOT classified (including look-alike prefixes,
   different extensions, and case variations);
3. pre-existing critical-file behavior is unchanged (the original eight
   basenames and the manual-review gate);
4. the set is exactly the union (no accidental additions/removals);
5. mixed classification keeps each file on its own side.
"""
from __future__ import annotations

from aipm.services.git.conflicts import ConflictAnalyzer


EXPECTED_ORIGINAL = {
    "docker-compose.yml",
    "compose.yaml",
    "compose.yml",
    "Dockerfile",
    "start_services.py",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
}

EXPECTED_SECRET_FILES = {".env", ".env.local", ".env.local.backup"}


def test_secret_basenames_classified_critical():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify(
        [".env", ".env.local", ".env.local.backup", "src/app.py"]
    )
    assert classified["critical"] == [".env", ".env.local", ".env.local.backup"]
    assert classified["normal"] == ["src/app.py"]


def test_secret_paths_with_directories_classified_critical():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify(
        [
            "deploy/.env",
            "apps/web/.env.local",
            "deep/nested/dir/.env.local.backup",
        ]
    )
    # Exact-basename matching: the directory part never changes the verdict.
    # Classification preserves input order.
    assert classified["critical"] == [
        "deploy/.env",
        "apps/web/.env.local",
        "deep/nested/dir/.env.local.backup",
    ]
    assert classified["normal"] == []


def test_unrelated_filenames_not_classified_critical():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify(
        [
            ".envrc",          # direnv look-alike: different basename
            "env",             # no dot prefix
            ".env.example",    # template, not a secret file
            ".environment",    # prefix look-alike
            "env.local",       # missing dot
            ".env.local.orig", # different suffix
            "dotenv",          # unrelated word
            "app.py",
            "README.md",
        ]
    )
    assert classified["critical"] == []
    assert len(classified["normal"]) == 9


def test_original_critical_files_behavior_unchanged():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify(
        [
            "docker-compose.yml",
            "compose.yaml",
            "compose.yml",
            "Dockerfile",
            "start_services.py",
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "main.py",
        ]
    )
    assert sorted(classified["critical"]) == sorted(EXPECTED_ORIGINAL)
    assert classified["normal"] == ["main.py"]


def test_case_sensitivity_preserved_for_secret_files():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify([".ENV", ".Env.LOCAL", ".env.LOCAL.BACKUP"])
    assert classified["critical"] == []
    assert len(classified["normal"]) == 3


def test_requires_manual_review_flags_secret_files():
    analyzer = ConflictAnalyzer()
    assert analyzer.requires_manual_review([".env"]) is True
    assert analyzer.requires_manual_review([".env.local"]) is True
    assert analyzer.requires_manual_review([".env.local.backup"]) is True
    assert analyzer.requires_manual_review(["app.py"]) is False


def test_critical_files_set_is_exact_union():
    assert ConflictAnalyzer.CRITICAL_FILES == EXPECTED_ORIGINAL | EXPECTED_SECRET_FILES


def test_mixed_classification_keeps_sides_separate():
    analyzer = ConflictAnalyzer()
    classified = analyzer.classify(
        ["package.json", ".env", "requirements.txt", "src/index.js"]
    )
    assert classified["critical"] == ["package.json", ".env", "requirements.txt"]
    assert classified["normal"] == ["src/index.js"]
