"""Execute sibling skill entry points in isolated processes."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path
from typing import Sequence

def run_skill_script(
    project_root: Path,
    skill_name: str,
    script_name: str,
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a terminal skill in its own process and release its working memory."""

    project_root = project_root.resolve()
    skill_root = Path(__file__).resolve().parents[3]
    script_path = skill_root / skill_name / "scripts" / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"skill script not found: {script_path.as_posix()}")
    command = [sys.executable, script_path.as_posix(), *arguments]
    if not capture_output:
        return subprocess.run(
            command,
            cwd=project_root,
            check=True,
            text=True,
        )

    process = subprocess.Popen(
        command,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            output.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        process.stdout.close()
    return_code = process.wait()
    completed_output = "".join(output)
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code,
            command,
            output=completed_output,
        )
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=completed_output,
        stderr="",
    )
