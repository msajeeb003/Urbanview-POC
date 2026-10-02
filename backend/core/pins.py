"""Library versions: ``backend/constraints.txt`` against what an environment has installed.

``pyproject.toml`` states the ranges the code accepts; ``constraints.txt`` names the one version
of every library that is in use. Both images install with ``pip install -c constraints.txt`` and a
developer's environment does too, so the server runs the versions the tests ran on, and a deploy
never picks up a release nobody tested (2026-10-03: the server had SQLAlchemy 2.1, FastAPI 0.142
and Starlette 1.7 while the tests ran on 2.0, 0.141 and 1.6).

    python -m core.pins            # every listed library that is installed has its pinned version
    python -m core.pins --strict   # ... and nothing is installed that the file does not list

Exit 0 when they agree, 1 with the differences otherwise. The image builds run ``--strict`` (an
image holds the project's libraries and nothing else), so a library that came in unlisted (the
new dependency of a library whose version was raised) stops the build and names the line to add.
A developer's environment may hold more (a local S3 stand-in, platform packages): there only the
listed libraries are compared, which ``tests/test_pins.py`` does on every test run.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

CONSTRAINTS = Path(__file__).resolve().parents[1] / "constraints.txt"
# the image's own tools and the project itself are not libraries the file pins
UNPINNED = frozenset({"pip", "setuptools", "wheel", "urbanview-backend"})


def normal(name: str) -> str:
    """A distribution name as pip compares it: ``pydantic_core`` = ``Pydantic-Core``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path = CONSTRAINTS) -> dict[str, str]:
    """``{name: version}`` of the constraints file; anything but ``name==version`` is an error."""
    pins: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        row = line.split("#", 1)[0].strip()
        if not row:
            continue
        name, equals, version = row.partition("==")
        if not equals or not name.strip() or not version.strip() or " " in version.strip():
            raise ValueError(f"{path.name} line {number}: expected `name==version`, got {row!r}")
        key = normal(name.strip())
        if key in pins:
            raise ValueError(f"{path.name} line {number}: {name.strip()} is listed twice")
        pins[key] = version.strip()
    return pins


def installed() -> dict[str, str]:
    """``{name: version}`` of every distribution in this environment."""
    found: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name:
            found[normal(name)] = distribution.version
    return found


@dataclass(frozen=True)
class Differences:
    #: (name, pinned version, installed version)
    different: tuple[tuple[str, str, str], ...]
    #: (name, installed version): installed, and the file does not list it
    unlisted: tuple[tuple[str, str], ...]
    #: listed libraries that are installed at their pinned version
    matching: int
    #: listed libraries this environment does not have (another platform's, another image's)
    absent: int

    def ok(self, *, strict: bool) -> bool:
        return not self.different and not (strict and self.unlisted)


def compare(pins: dict[str, str], have: dict[str, str]) -> Differences:
    different = tuple(
        (name, pins[name], have[name])
        for name in sorted(pins)
        if name in have and have[name] != pins[name]
    )
    unlisted = tuple(
        (name, have[name]) for name in sorted(have) if name not in pins and name not in UNPINNED
    )
    matching = sum(1 for name in pins if have.get(name) == pins[name])
    absent = sum(1 for name in pins if name not in have)
    return Differences(different=different, unlisted=unlisted, matching=matching, absent=absent)


def report(found: Differences, *, strict: bool, file_name: str = CONSTRAINTS.name) -> str:
    lines: list[str] = []
    if found.different:
        lines.append(f"Installed versions differ from {file_name}:")
        lines += [f"  {name}: {pin} pinned, {has} installed" for name, pin, has in found.different]
        lines.append(f'  -> pip install -c {file_name} -e ".[dev,gis,ai]"')
    if strict and found.unlisted:
        lines.append(f"Installed and not listed in {file_name} (add the line, test, commit):")
        lines += [f"  {name}=={version}" for name, version in found.unlisted]
    if not lines:
        absent = (
            "1 pinned library is" if found.absent == 1 else f"{found.absent} pinned libraries are"
        )
        lines.append(
            f"{found.matching} libraries at their pinned versions"
            + (f" ({absent} not installed here)" if found.absent else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    unknown = [a for a in arguments if a != "--strict"]
    if unknown:
        print(f"usage: python -m core.pins [--strict] (unknown: {' '.join(unknown)})")
        return 2
    strict = "--strict" in arguments
    found = compare(read_pins(), installed())
    print(report(found, strict=strict))
    return 0 if found.ok(strict=strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
