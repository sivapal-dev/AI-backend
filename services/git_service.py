import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from services.code_fixer import PROJECT_ROOT


class GitService:
    def __init__(self, repo_root: Path = PROJECT_ROOT) -> None:
        self.repo_root = repo_root

    def _run(self, args: List[str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def get_current_branch(self) -> str:
        branch = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        return branch or "main"

    def ensure_branch(self, branch_name: str) -> str:
        existing = self._run(["branch", "--list", branch_name])
        if existing.strip():
            self._run(["checkout", branch_name])
        else:
            self._run(["checkout", "-b", branch_name])
        return branch_name

    def stage_files(self, file_paths: Iterable[str]) -> None:
        cleaned = [path for path in file_paths if path]
        if cleaned:
            self._run(["add", *cleaned])

    def commit(self, message: str) -> str:
        self._run(["commit", "-m", message])
        return self._run(["rev-parse", "HEAD"])

    def has_staged_changes(self) -> bool:
        return bool(self._run(["diff", "--cached", "--name-only"]))

    def push_to_remote(self, branch_name: str, github_repo: str, github_token: str) -> None:
        """Push the local branch to the specified GitHub repository securely without leaking the token in argv."""
        import tempfile
        import os

        # We construct the remote URL with a dummy username 'x-access-token' instead of the token itself
        remote_url = f"https://x-access-token@github.com/{github_repo}.git"
        
        # Create a temporary askpass script to feed the token to Git securely when requested
        is_windows = os.name == "nt"
        with tempfile.TemporaryDirectory() as tmpdir:
            if is_windows:
                askpass_path = os.path.join(tmpdir, "askpass.bat")
                with open(askpass_path, "w", encoding="utf-8") as f:
                    f.write("@echo %GITHUB_TOKEN%\n")
            else:
                askpass_path = os.path.join(tmpdir, "askpass.sh")
                with open(askpass_path, "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\necho \"$GITHUB_TOKEN\"\n")
                os.chmod(askpass_path, 0o700)

            env = os.environ.copy()
            env["GIT_ASKPASS"] = askpass_path
            env["GITHUB_TOKEN"] = github_token
            # Set GIT_TERMINAL_PROMPT=0 to prevent hanging on interactive terminal prompt
            env["GIT_TERMINAL_PROMPT"] = "0"

            try:
                subprocess.run(
                    ["git", "push", remote_url, branch_name],
                    cwd=self.repo_root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                # Clean the token from the error message before raising (just in case)
                safe_error = exc.stderr.replace(github_token, "***TOKEN***")
                raise RuntimeError(f"Git push failed: {safe_error}")


git_service = GitService()
