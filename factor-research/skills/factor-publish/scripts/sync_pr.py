"""Create or synchronize a factor Draft PR description from its committed README."""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


def gh(*arguments: str) -> str:
    return subprocess.run(
        ["gh", *arguments], check=True, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout


def compose_body(readme: str, previous: str = "", issue: int | None = None) -> str:
    if not readme.strip():
        raise ValueError("Factor README is empty")
    if issue is not None:
        prefix = f"Closes #{issue}\n\n"
    else:
        references = re.findall(
            r"(?mi)^[ \t\ufeff]*(?:[-*][ \t]+)?"
            r"((?:Closes|Fixes|Resolves|Related to) #[1-9][0-9]*)"
            r"[.\u3002]?[ \t\r]*$", previous,
        )
        prefix = "\n".join(dict.fromkeys(references)) + "\n\n" if references else ""
    return prefix + readme


def sync(args: argparse.Namespace) -> None:
    repo = args.repo or gh("repo", "view", "--json", "nameWithOwner",
                           "--jq", ".nameWithOwner").strip()
    number = args.pr
    if args.factor_name:
        matches = json.loads(gh("pr", "list", "--repo", repo, "--state", "open",
                                "--head", args.factor_name, "--base", "main",
                                "--json", "number"))
        if len(matches) > 1:
            raise ValueError("More than one open factor PR")
        number = matches[0]["number"] if matches else None
        if number is None and not args.create:
            raise ValueError("No open factor PR; use --create after pushing the branch")
    if number is not None:
        pr = json.loads(gh("api", f"repos/{repo}/pulls/{number}"))
        if (pr["state"] != "open" or not pr["draft"]
                or pr["head"]["repo"]["full_name"] != repo
                or pr["base"]["ref"] != "main"):
            print("Skipped: only same-repository Draft PRs targeting main are synchronized")
            return
        factor = pr["head"]["ref"]
        sha = pr["head"]["sha"]
        previous = pr.get("body") or ""
    else:
        factor = args.factor_name
        sha = json.loads(gh("api", f"repos/{repo}/commits/{quote(factor, safe='')}"))["sha"]
        previous = ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factor):
        print("Skipped: branch is not a factor package name")
        return
    endpoint = f"repos/{repo}/contents/factor_zoo/{factor}/README.md?ref={sha}"
    try:
        content = json.loads(gh("api", endpoint))
    except subprocess.CalledProcessError as exc:
        if args.pr and "404" in exc.stderr:
            print("Skipped: branch has no factor README")
            return
        raise
    readme = base64.b64decode(content["content"]).decode("utf-8")
    body = compose_body(readme, previous, args.issue)
    if number is not None and body == previous:
        print(f"Draft PR #{number} description already matches README at {sha}")
        return
    if args.dry_run:
        print(f"Would synchronize {factor} README at {sha}; create={number is None}")
        return
    with tempfile.TemporaryDirectory(prefix="factor_pr_") as directory:
        path = Path(directory) / "body.txt"
        path.write_text(body, encoding="utf-8", newline="")
        if number is None:
            print(gh("pr", "create", "--repo", repo, "--draft", "--base", "main",
                     "--head", factor, "--title", factor, "--body-file", str(path)).strip())
        else:
            print(gh("pr", "edit", str(number), "--repo", repo,
                     "--body-file", str(path)).strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--factor-name")
    target.add_argument("--pr", type=int)
    parser.add_argument("--repo")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.issue is not None and args.issue < 1:
        parser.error("--issue must be positive")
    if args.create and not args.factor_name:
        parser.error("--create requires --factor-name")
    sync(args)


if __name__ == "__main__":
    main()
