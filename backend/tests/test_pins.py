"""The pinned library versions (`constraints.txt`, `core.pins`): the tests run on the versions the
server installs, every library the project names is pinned, and the checker tells a wrong
version from a library the file does not list."""

import re
import tomllib
from pathlib import Path

import pytest

from core.pins import CONSTRAINTS, compare, installed, main, normal, read_pins, report

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_tests_run_on_the_pinned_versions():
    """A library installed here at another version than the server's is a test run that proves
    nothing about the server: install with `pip install -c constraints.txt -e ".[dev,gis,ai]"`."""
    found = compare(read_pins(), installed())
    assert not found.different, report(found, strict=False)
    assert found.matching > 0


def test_every_library_the_project_names_is_pinned():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    named = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        named += extra
    pins = read_pins()
    missing = []
    for requirement in named:
        name = re.match(r"[A-Za-z0-9_.\-]+", requirement).group(0)
        if normal(name) not in pins:
            missing.append(name)
    assert not missing, f"not in {CONSTRAINTS.name}: {missing}"


def test_names_are_compared_the_way_pip_compares_them():
    assert normal("pydantic_core") == normal("Pydantic-Core") == "pydantic-core"
    assert normal("python-dateutil") == "python-dateutil"
    pins = read_pins()
    assert "sqlalchemy" in pins and "geoalchemy2" in pins and "typing-extensions" in pins


def test_a_wrong_version_and_an_unlisted_library_are_told_apart():
    pins = {"fastapi": "0.142.2", "sqlalchemy": "2.1.2", "uvloop": "0.23.0"}
    have = {"fastapi": "0.142.2", "sqlalchemy": "2.0.54", "moto": "5.1.0", "pip": "24.0"}
    found = compare(pins, have)
    assert found.different == (("sqlalchemy", "2.1.2", "2.0.54"),)
    # pip is the image's own tool; uvloop is another platform's library (not installed here)
    assert found.unlisted == (("moto", "5.1.0"),)
    assert (found.matching, found.absent) == (1, 1)
    assert not found.ok(strict=False) and not found.ok(strict=True)
    text = report(found, strict=True)
    assert "sqlalchemy: 2.1.2 pinned, 2.0.54 installed" in text and "moto==5.1.0" in text

    # a developer's environment may hold more than the file lists; an image may not
    extra_only = compare(pins, {"fastapi": "0.142.2", "sqlalchemy": "2.1.2", "moto": "5.1.0"})
    assert extra_only.ok(strict=False) and not extra_only.ok(strict=True)
    assert "moto" not in report(extra_only, strict=False)
    exact = compare(pins, {"fastapi": "0.142.2", "sqlalchemy": "2.1.2", "uvloop": "0.23.0"})
    assert exact.ok(strict=True)
    assert report(exact, strict=True) == "3 libraries at their pinned versions"


def test_the_file_accepts_only_exact_versions(tmp_path):
    good = tmp_path / "constraints.txt"
    good.write_text(
        "# a comment\n\nFastAPI==0.142.2  # why\npydantic_core==2.46.5\n", encoding="utf-8"
    )
    assert read_pins(good) == {"fastapi": "0.142.2", "pydantic-core": "2.46.5"}
    for bad in ("fastapi>=0.115\n", "fastapi\n", "fastapi==\n", "a==1\nA==2\n"):
        path = tmp_path / "bad.txt"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            read_pins(path)


def test_the_command_answers_with_an_exit_code(capsys):
    assert main([]) == 0
    assert "libraries at their pinned versions" in capsys.readouterr().out
    assert main(["--nope"]) == 2
