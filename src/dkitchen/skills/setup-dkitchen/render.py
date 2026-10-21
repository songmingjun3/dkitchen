#!/usr/bin/env python3
"""Render the kitchen templates for a chosen set of modules.

Usage: render.py --modules run-budgets,unattended --out docs/agents [--kitchen DIR]

Writes the core rules and the chosen modules' files into --out, keeping each chosen module's marked text
and dropping the others', with every module marker removed. Prints the rendered AGENTS.md and CLAUDE.md
blocks. `<!-- fill: … -->` gaps are left in place for the setup skill to fill.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

MODULES = ("run-budgets", "model-text", "artifacts", "unattended")
CORE = ("roles.md", "issue-tracker.md", "messaging.md", "long-running-jobs.md")
INSTALLED = {"run-budgets": "run-budgets.md", "model-text": "model-text.md",
             "artifacts": "artifacts.md", "unattended": "unattended.md"}
SECTION = re.compile(r"<!-- module:([\w-]+) -->(.*?)<!-- /module:\1 -->", re.S)


def render(text: str, modules: set[str]) -> str:
    unknown = {name for name, _ in SECTION.findall(text)} - set(MODULES)
    if unknown:
        raise ValueError(f"unknown module markers: {', '.join(sorted(unknown))}")
    text = re.sub(r"^[ \t]*(<!-- /?module:[\w-]+ -->)[ \t]*\n", r"\1", text, flags=re.M)  # a marker line leaves no line
    text = SECTION.sub(lambda match: match.group(2) if match.group(1) in modules else "", text)
    if "<!-- module:" in text or "<!-- /module:" in text:
        raise ValueError("unbalanced module markers")
    return re.sub(r"\n{3,}", "\n\n", text)


def blocks(text: str) -> dict[str, str]:
    """The fenced Markdown blocks of agents-block.md, keyed by the heading above each."""
    return {heading: body.strip("\n") for heading, body in
            re.findall(r"^## (\S+)\n\n```markdown\n(.*?)^```", text, flags=re.S | re.M)}


def find_kitchen(explicit: Path | None) -> Path:
    """The `kitchen/` template folder that ships inside the installed `dkitchen` package.

    The templates travel with the package, so an installed skill renders them without a
    checkout. The search order is: the explicit flag, the package this skill belongs to
    (which covers both a checkout and a wheel), the repository it was installed into, and
    finally a `kitchen/` beside the current directory.
    """
    if explicit is not None:
        if not (explicit / "README.md").is_file():
            raise ValueError(f"{explicit} is not a kitchen folder: no README.md")
        return explicit
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "kitchen",                      # dkitchen/skills/setup-dkitchen/ -> dkitchen/kitchen
        Path.cwd() / "kitchen",
    ]
    try:
        import dkitchen

        candidates.append(Path(dkitchen.__file__).resolve().parent / "kitchen")
    except ImportError:  # pragma: no cover - the skill may be used without the package
        pass
    for candidate in candidates:
        if candidate is not None and (candidate / "README.md").is_file():
            return candidate
    raise ValueError("cannot find the kitchen/ templates; pass --kitchen DIR, or reinstall "
                     "dkitchen so the package carries them")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modules", default="", help=f"comma-separated, from: {', '.join(MODULES)}")
    parser.add_argument("--out", type=Path, required=True, help="folder for the rendered rules, such as docs/agents")
    parser.add_argument("--kitchen", type=Path,
                        help="the dkitchen kitchen/ folder (default: the one inside the installed "
                             "package, else beside the current directory)")
    args = parser.parse_args()
    modules = {name.strip() for name in args.modules.split(",") if name.strip()}
    if modules - set(MODULES):
        parser.error(f"unknown modules: {', '.join(sorted(modules - set(MODULES)))}")
    try:
        kitchen = find_kitchen(args.kitchen)
        sources = [kitchen / "core" / name for name in CORE]
        sources += [kitchen / "modules" / INSTALLED[name] for name in MODULES if name in modules]
        args.out.mkdir(parents=True, exist_ok=True)
        for source in sources:
            (args.out / source.name).write_text(render(source.read_text(encoding="utf-8"), modules), encoding="utf-8")
            print(f"wrote {args.out / source.name}", file=sys.stderr)
        for heading, body in blocks(render((kitchen / "core" / "agents-block.md").read_text(encoding="utf-8"), modules)).items():
            print(f"## {heading}\n\n{body}\n")
    except (OSError, ValueError) as error:
        print(f"render.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
