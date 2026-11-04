from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from .protocol import FactorPackage


def calculation_sources(package: FactorPackage) -> list[Path]:
    """Follow local imports from the public calculation entry point."""
    root = package.factor_root.resolve()
    entry = package.module_path.resolve()
    if not entry.is_file():
        return []
    pending = [entry]
    pending.extend(parent / '__init__.py' for parent in entry.parents
                   if parent.is_relative_to(root) and (parent / '__init__.py').is_file())
    visited: set[Path] = set()

    def modules(base: Path, parts: list[str]) -> list[Path]:
        target = base.joinpath(*parts)
        candidates = [target.with_suffix('.py'), target / '__init__.py']
        candidates.extend(parent / '__init__.py' for parent in target.parents
                          if parent.is_relative_to(root))
        return [p.resolve() for p in candidates if p.is_file() and p.is_relative_to(root)]

    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        nodes = list(ast.walk(tree))
        if path == entry:
            # Main also wires validation callbacks. Follow calculation bindings only.
            bindings: dict[str, ast.AST] = {}
            statements = list(tree.body)
            for statement in statements:
                if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for field in ('body', 'orelse', 'finalbody'):
                        statements.extend(getattr(statement, field, ()))
                    for handler in getattr(statement, 'handlers', ()):
                        statements.extend(handler.body)
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bindings[statement.name] = statement
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        for node in ast.walk(target):
                            if isinstance(node, ast.Name):
                                bindings[node.id] = statement
            names = {'calculate_factor', 'FACTOR_NAME', 'FACTOR_COLUMNS',
                     'UPDATE_CALENDAR', 'UPDATE_CALENDAR_TABLES', 'UPDATE_SOURCE_TABLES',
                     'SUPPORTS_COMPUTE_BATCH_DAYS'}
            remaining = list(names)
            selected: list[ast.AST] = []
            while remaining:
                statement = bindings.get(remaining.pop())
                if statement is None or statement in selected:
                    continue
                selected.append(statement)
                for node in ast.walk(statement):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in names:
                        names.add(node.id)
                        remaining.append(node.id)
            nodes = [node for statement in selected for node in ast.walk(statement)]
            nodes.extend(node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
        else:
            names = None

        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                aliases = [alias for alias in node.names
                           if names is None or alias.name == '*' or (alias.asname or alias.name) in names]
                if not aliases:
                    continue
                if node.level:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    parts = node.module.split('.') if node.module else []
                else:
                    base = root.parent
                    parts = (node.module or '').split('.')
                    if parts[:1] == [root.parent.name]:
                        base = root.parent.parent
                pending.extend(modules(base, parts))
                for alias in aliases:
                    if alias.name != '*':
                        pending.extend(modules(base, parts + alias.name.split('.')))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if names is not None and (alias.asname or alias.name.split('.')[0]) not in names:
                        continue
                    parts = alias.name.split('.')
                    base = root.parent.parent if parts[:1] == [root.parent.name] else root.parent
                    pending.extend(modules(base, parts))
    return sorted(visited)


def factor_definition_fingerprint(package: FactorPackage) -> str:
    """Hash relative source paths and source contents with normalized newlines.

    Only calculation dependencies within the factor package are included.
    """
    paths = calculation_sources(package)

    digest = hashlib.sha256()
    for path in paths:
        relative = (
            path.relative_to(package.factor_root).as_posix()
            if path.is_relative_to(package.factor_root)
            else path.name
        )
        source = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source)
        digest.update(b"\0")
    return digest.hexdigest()
