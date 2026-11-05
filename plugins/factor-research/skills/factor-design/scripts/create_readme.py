"""Create or update a templated factor README and its Release."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from factor_protocol import resolve_factor_zoo_root
from factor_protocol.readme import acceptance_items, set_acceptance
from validate_readme import validate


def _render_readme(
    text: str, *, title: str, motivation: str, definition: str,
    parameters: str, references: str, no_reference_pdf: str | None = None,
) -> str:
    items = acceptance_items(text)
    if len(items) != 8:
        raise ValueError("factor README must contain eight acceptance items")
    sections = {1: motivation, 2: definition, 3: parameters, 5: references}
    if any(not content.strip() for content in sections.values()):
        raise ValueError("motivation, definition, parameters and references must not be empty")
    references = re.sub(r"(?m)^Reference PDFs:.*\n?", "", references).strip()
    if no_reference_pdf is not None:
        reason = " ".join(no_reference_pdf.split())
        if not reason:
            raise ValueError("no-reference-pdf requires a non-empty reason")
        references += "\n\nReference PDFs: " + reason
    sections[5] = references
    result = re.sub(r"(?m)^# [^\n]+$", lambda _: "# " + title, text, count=1)
    definition_changed = False
    for number, content in sections.items():
        section = re.search(rf"(?ms)(^## {number}[.] [^\n]+\n).*?(?=^## |\Z)", result)
        if section is None:
            raise ValueError(f"README is missing section {number}")
        replacement = section.group(1) + "\n" + content.strip() + "\n\n"
        if number <= 3 and section.group().rstrip() != replacement.rstrip():
            definition_changed = True
        result = result[:section.start()] + replacement + result[section.end():]
    if definition_changed:
        for item in range(1, 9):
            result = set_acceptance(result, item, False)
    return result


def create_readme(
    *, title: str, motivation: str, definition: str,
    parameters: str, references: str, project_root: str | Path = ".",
    reference_pdfs: Sequence[str | Path] | None = None,
    no_reference_pdf: str | None = None, repo: str | None = None,
    target: str | None = None, dry_run: bool = False,
) -> Path:
    if not re.fullmatch(r"[A-Z0-9_][A-Za-z0-9_]*(?: [A-Z0-9_][A-Za-z0-9_]*)*", title):
        raise ValueError("title must use capitalized English words, digits or underscores separated by spaces")
    factor_name = title.lower().replace(" ", "_")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factor_name):
        raise ValueError("title must produce a valid Python identifier")
    if bool(reference_pdfs) == (no_reference_pdf is not None):
        raise ValueError("provide reference PDFs or a user-confirmed absence reason")
    root = Path(project_root).resolve()
    readme = resolve_factor_zoo_root(project_root=root) / factor_name / "README.md"
    template = Path(__file__).resolve().parents[1] / "assets/factor_readme_template.md"
    original = readme.read_text(encoding="utf-8") if readme.exists() else template.read_text(encoding="utf-8")
    notes = _render_readme(original, title=title, motivation=motivation, definition=definition,
                           parameters=parameters, references=references, no_reference_pdf=no_reference_pdf)
    repo_args = ["--repo", repo] if repo else []
    with tempfile.TemporaryDirectory(prefix="factor_design_") as directory:
        stage = Path(directory)
        notes_file = stage / "README.md"
        notes_file.write_text(notes, encoding="utf-8", newline="\n")
        errors = validate(notes_file)
        if errors:
            raise ValueError("; ".join(errors))
        paths = []
        for index, value in enumerate(reference_pdfs or [], 1):
            source = Path(value)
            source = source if source.is_absolute() else root / source
            with source.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError(f"invalid reference PDF: {source}")
            path = stage / f"{factor_name}_reference_{index:02d}.pdf"
            shutil.copyfile(source, path)
            paths.append(path)
        result = subprocess.run(
            ["gh", "release", "view", factor_name, *repo_args, "--json", "isPrerelease,isDraft"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
        )
        existing = result.returncode == 0
        prerelease = True
        if existing:
            state = json.loads(result.stdout)
            prerelease = state["isPrerelease"]
            if state["isDraft"]:
                raise ValueError("Design requires a non-draft Release")
            command = ["gh", "release", "edit", factor_name, *repo_args,
                       "--title", title, "--notes-file", str(notes_file)]
        else:
            endpoint = f"repos/{repo}/releases/tags/{factor_name}" if repo else "repos/{owner}/{repo}/releases/tags/" + factor_name
            lookup = subprocess.run(["gh", "api", endpoint], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8")
            if lookup.returncode == 0 or "404" not in lookup.stderr:
                raise RuntimeError(lookup.stderr.strip() or result.stderr.strip() or "cannot inspect release")
            command = ["gh", "release", "create", factor_name, *repo_args,
                       "--prerelease", "--latest=false", "--target", target or factor_name,
                       "--title", title, "--notes-file", str(notes_file), *map(str, paths)]
        if dry_run:
            print(f"Would create or update README and Release: {factor_name} | {readme.as_posix()}")
            return readme
        readme.parent.mkdir(parents=True, exist_ok=True)
        if not readme.exists() or readme.read_text(encoding="utf-8") != notes:
            readme.write_text(notes, encoding="utf-8", newline="\n")
        if existing and paths:
            subprocess.run(["gh", "release", "upload", factor_name, *repo_args,
                            "--clobber", *map(str, paths)], cwd=root, check=True)
        subprocess.run(command, cwd=root, check=True)
        result = subprocess.run(
            ["gh", "release", "view", factor_name, *repo_args,
             "--json", "tagName,name,body,isPrerelease,isDraft,assets"],
            cwd=root, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        state = json.loads(result.stdout)
        remote = {asset["name"]: asset for asset in state["assets"]}
        if (state["tagName"] != factor_name or state["name"] != title
                or state["body"] != notes or state["isPrerelease"] != prerelease or state["isDraft"]
                or any(path.name not in remote or remote[path.name]["size"] != path.stat().st_size for path in paths)):
            raise ValueError("Design README or Release verification failed")
        print(f"Design: pass | factor={factor_name} | reference_pdfs={len(paths)}")
        print(f"README: {readme.as_posix()}")
    return readme


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--title", required=True, help="Capitalized English title with digits or underscores; its lowercase, space-to-underscore form must be a Python identifier.")
    parser.add_argument("--motivation", required=True, help="Markdown body for section 1.")
    parser.add_argument("--definition", required=True, help="Markdown body for section 2.")
    parser.add_argument("--parameters", required=True, help="Markdown body for section 3, including parameters and output columns.")
    parser.add_argument("--references", required=True, help="Markdown body for section 5.")
    parser.add_argument("--repo")
    parser.add_argument("--target", help="Git commit or branch; defaults to the factor branch.")
    references = parser.add_mutually_exclusive_group(required=True)
    references.add_argument("--reference-pdf", action="append", type=Path)
    references.add_argument("--no-reference-pdf", help="User-confirmed reason why reference PDFs are unavailable.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        create_readme(title=args.title, motivation=args.motivation,
                      definition=args.definition, parameters=args.parameters, references=args.references,
                      project_root=args.project_root, reference_pdfs=args.reference_pdf,
                      no_reference_pdf=args.no_reference_pdf, repo=args.repo, target=args.target, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        reason = (exc.stderr or str(exc)) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Design failed: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
