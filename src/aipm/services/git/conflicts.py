from pathlib import Path


class ConflictAnalyzer:

    CRITICAL_FILES = {
        "docker-compose.yml",
        "compose.yaml",
        "compose.yml",
        "Dockerfile",
        "start_services.py",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
    }

    def classify(self, files: list[str]) -> dict:
        critical = []
        normal = []

        for file in files:
            name = Path(file).name
            if name in self.CRITICAL_FILES:
                critical.append(file)
            else:
                normal.append(file)

        return {
            "critical": critical,
            "normal": normal,
        }

    def requires_manual_review(self, files: list[str]) -> bool:
        classified = self.classify(files)
        return bool(classified["critical"])