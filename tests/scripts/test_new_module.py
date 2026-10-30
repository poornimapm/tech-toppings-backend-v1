"""`poe new-module` (scripts/new_module.py, ADR-0020): a spec is checked before anything is
written, nothing is ever written over, and the module it makes works through the real app (its
routes, its tile, only its owner's items) in the project's own style."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from app.core.config import PROJECT_ROOT, ModulesSettings
from app.main import create_app
from scripts.new_module import (
    FRONTEND,
    RESERVED,
    SpecError,
    backend_files,
    load_spec,
    main,
    write_backend,
)
from tests.helpers import (
    TEST_MODULE_PACKAGES,
    api_operations,
    build_settings,
    register,
    running_client,
)

EXAMPLE = PROJECT_ROOT / "docs" / "modules" / "example-module.yaml"
Change = Callable[[dict[str, Any]], object]


def spec_file(tmp_path: Path, change: Change = lambda _: None) -> Path:
    """The example spec, changed by ``change``."""
    spec = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    change(spec)
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return path


def test_the_example_spec_makes_a_module() -> None:
    spec = load_spec(EXAMPLE)

    assert (spec.key, spec.slug, spec.camel, spec.status) == ("plants", "plants", "Plants", "beta")
    assert spec.tokens()["module"] == "app.modules.plants"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda spec: spec.update(key="Plants"), "key"),
        (lambda spec: spec.update(key="x"), "key"),
        (lambda spec: spec.update(key="a" * 25), "key"),
        (lambda spec: spec.update(key="house__plants"), "key"),  # -> HousePlants, as house_plants
        (lambda spec: spec.update(key="plants_"), "key"),
        (lambda spec: spec.update(key="plants_2"), "key"),  # -> Plants2, as plants2
        (lambda spec: spec.update(key="class"), "Python keyword"),
        (lambda spec: spec.update(key="admin"), "belongs to the platform"),
        (lambda spec: spec.update(key="modules"), "belongs to the platform"),
        (lambda spec: spec.update(accent="pink"), "accent"),
        (lambda spec: spec.update(status="coming_soon"), "status"),
        (lambda spec: spec.update(icon="sprout"), "icon"),
        (lambda spec: spec.update(order=-1), "order"),
        (lambda spec: spec.update(colour="red"), "colour"),
        (lambda spec: spec["strings"].pop("ta"), "missing: ta"),
        (lambda spec: spec["strings"]["hi"].update(name=""), "strings.hi.name"),
        (
            lambda spec: spec["strings"]["ta"].update(tagline=spec["strings"]["en"]["tagline"]),
            "still in English: ta.tagline",
        ),
    ],
)
def test_a_spec_that_cant_make_a_module_is_refused(
    tmp_path: Path, change: Change, message: str
) -> None:
    with pytest.raises(SpecError, match=message):
        load_spec(spec_file(tmp_path, change))


def test_texts_are_tidied_and_made_safe_for_docstrings(tmp_path: Path) -> None:
    spec = load_spec(
        spec_file(
            tmp_path, lambda spec: spec["strings"]["en"].update(name='  Two \t "odd"  \\ words ')
        )
    )

    assert spec.strings["en"].name == 'Two "odd" \\ words'
    assert spec.tokens()["name"] == "Two 'odd' / words"


def test_the_reserved_keys_are_every_platform_path(app: FastAPI) -> None:
    modules = {module.manifest.slug for module in app.state.modules}
    platform = {path.split("/")[2] for _, path in api_operations(app)} - modules

    assert platform <= RESERVED


def test_the_test_modules_key_is_never_taken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = spec_file(tmp_path, lambda spec: spec.update(key="notes"))

    # In this very project: it refuses before writing anything.
    code = main(["--spec", str(spec), "--backend-only", "--no-check"])

    assert code == 1
    assert str(PROJECT_ROOT / "tests" / "fixtures" / "modules" / "notes") in capsys.readouterr().out


def test_a_module_already_there_is_never_written_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "app" / "modules" / "plants"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("mine", encoding="utf-8")

    code = main(["--spec", str(EXAMPLE), "--backend-only", "--no-check"], root=tmp_path)

    assert code == 1
    assert "is there already" in capsys.readouterr().out
    assert [path.name for path in existing.iterdir()] == ["keep.txt"]
    assert not (tmp_path / "tests").exists()


def frontend_ready() -> bool:
    """Node 24 or later, and the frontend repository beside this one with its packages."""
    node = shutil.which("node")
    if node is None or not (FRONTEND / "node_modules" / "lucide-react").is_dir():
        return False
    version = subprocess.run(  # noqa: S603 - Node's own version
        [node, "--version"], capture_output=True, text=True, check=False
    ).stdout
    major = re.match(r"v(\d+)", version)
    return major is not None and int(major.group(1)) >= 24


@pytest.mark.skipif(not frontend_ready(), reason="needs Node 24 and the frontend repository")
@pytest.mark.parametrize("only", [[], ["--backend-only"]], ids=["both", "backend-only"])
def test_the_frontend_checks_the_spec_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], only: list[str]
) -> None:
    # A new key (the frontend may have a module of the example's), and JavaScript's own Map.
    key = f"made_x{uuid.uuid4().hex[:6]}"
    spec = spec_file(tmp_path, lambda spec: spec.update(key=key, icon="Map"))

    code = main(["--spec", str(spec), "--no-check", *only], root=tmp_path)

    out = capsys.readouterr().out
    assert code == 1
    assert f"the frontend's check of {spec} failed" in out
    assert "use MapIcon" in out
    assert not (tmp_path / "app").exists()


async def test_the_module_it_makes_works_through_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo_uri: str
) -> None:
    package = f"tt_made_{uuid.uuid4().hex[:8]}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_spec(
        spec_file(tmp_path, lambda spec: spec.update(key=f"made_x{uuid.uuid4().hex[:6]}"))
    )
    items = f"/v1/{spec.slug}/items"

    write_backend(spec, tmp_path, package)
    modules = ModulesSettings(_env_file=None, packages=(*TEST_MODULE_PACKAGES, package))
    async with running_client(create_app(build_settings(mongo_uri, modules=modules))) as client:
        account = await register(client)
        added = await client.post(items, headers=account.headers, json={"text": "Tulsi"})
        listed = await client.get(items, headers=account.headers)
        view = await client.get(f"/v1/modules/{spec.key}", headers=account.headers)

    assert added.status_code == 201, added.text
    assert [item["text"] for item in listed.json()["items"]] == ["Tulsi"]
    assert (view.json()["status"], view.json()["stat"]["value"]) == ("beta", 1)
    made = [tmp_path / package / spec.key, tmp_path / "tests" / "modules" / spec.key]
    assert written(made[0]) == [
        "__init__.py",
        "manifest.py",
        "models.py",
        "router.py",
        "schemas.py",
        "service.py",
        "stats.py",
    ]
    assert written(made[1]) == [
        "__init__.py",
        "isolation.py",
        f"test_{spec.key}.py",
    ]


def written(folder: Path) -> list[str]:
    """What the command wrote there: Python adds `__pycache__` once the app has imported it."""
    return sorted(path.name for path in folder.iterdir() if path.name != "__pycache__")


def ruff(*args: str, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the project's own linter, on text this test made
        [sys.executable, "-m", "ruff", *args, "-"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        check=False,
    )


def longest(spec: dict[str, Any]) -> None:
    """The longest key and English texts a spec may have."""
    spec["key"] = "abcdefghijk_lmnopqrstuvw"
    spec["strings"]["en"].update(name="N" * 40, tagline="T" * 80)


def typographic(spec: dict[str, Any]) -> None:
    """English as people type it: curly quotes, dashes, accents (the linter's ambiguous marks)."""
    spec["strings"]["en"].update(
        name="Caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{LEFT DOUBLE QUOTATION MARK}plants",
        tagline="What\N{RIGHT SINGLE QUOTATION MARK}s growing \N{EN DASH} 2\N{MULTIPLICATION SIGN}"
        " a day \N{EM DASH} and more",
    )


@pytest.mark.parametrize(
    "change", [lambda _: None, longest, typographic], ids=["example", "longest", "typographic"]
)
def test_what_it_makes_passes_the_projects_lint(tmp_path: Path, change: Change) -> None:
    """Each file is linted as if it were in place (app/modules/<key>/...), as `poe lint` would."""
    files = backend_files(load_spec(spec_file(tmp_path, change)), PROJECT_ROOT)

    for path, source in files.items():
        name = str(path.relative_to(PROJECT_ROOT))
        formatted = ruff("format", "--stdin-filename", name, source=source)
        lint = ruff("check", "--stdin-filename", name, source=formatted.stdout)
        assert (formatted.returncode, lint.returncode) == (0, 0), f"{name}\n{lint.stdout}"
