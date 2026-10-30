"""Make a new module in both repositories from a spec file (docs/modules/new-module.md).

Checks the spec on both sides, writes the backend module and its tests here, then the frontend
module next door (its ``scripts/new-module.ts``), updates the frontend's API types, and runs
the new module's tests on both sides. It never writes over anything, and undoes what it wrote
if writing fails or is stopped; files that fail the final checks are kept, to look into.
Usage: ``poe new-module --spec docs/modules/<key>.yaml [--frontend <folder>] [--backend-only]
[--no-check]``
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.config import PROJECT_ROOT

TEMPLATES = Path(__file__).resolve().parent / "module_template"
SCHEMA = "openapi.json"  # the API's schema, written here for the frontend's types
PACKAGE = "app.modules"  # where modules live (tests write theirs elsewhere)
FRONTEND = PROJECT_ROOT.parent / "tech-toppings-frontend-v1"
# As the frontend has them (src/core/modules/types.ts ACCENTS).
Accent = Literal["emerald", "rose", "amber", "sky", "indigo", "orange", "violet", "teal", "slate"]
LANGUAGES = ("en", "ta", "hi")
# Text a translator would have changed, as the frontend's translation check reads it.
WORDY = re.compile(r"[a-z]{3,}.*\s.*[a-z]{3,}", re.IGNORECASE)
TOKEN = re.compile(r"%%(\w+)%%")
# The new module's own tests, and the shared checks a module joins.
BACKEND_CHECKS = ("tests/isolation", "tests/architecture", "tests/platform/modules")
FRONTEND_CHECKS = ("src/core/i18n/completeness.test.ts", "src/core/modules")
# The platform's own /v1 paths: a module there would sit behind them (tests/scripts checks this
# against the app's routes).
RESERVED = frozenset(
    {
        "activity",
        "admin",
        "agents",
        "auth",
        "dashboard",
        "fx",
        "internal",
        "me",
        "modules",
        "notifications",
        "push",
        "reminders",
        "search",
    }
)


# Typographic marks as the ASCII they stand for: the linter finds them ambiguous in a docstring.
QUOTES = [
    f"{side} {kind} QUOTATION MARK" for side in ("LEFT", "RIGHT") for kind in ("SINGLE", "DOUBLE")
]
MARKS = str.maketrans(
    dict.fromkeys(map(unicodedata.lookup, QUOTES), "'")
    | dict.fromkeys(map(unicodedata.lookup, ("EN DASH", "EM DASH", "MINUS SIGN")), "-")
    | {unicodedata.lookup("MULTIPLICATION SIGN"): "x"}
)


class SpecError(Exception):
    """A spec that can't make a module; the message says what to change."""


def _in_docstring(text: str) -> str:
    """Text as a docstring can hold it: ASCII ("café" -> "cafe", typographic quotes and dashes
    as plain ones), with no quote to end it and no backslash to escape."""
    text = unicodedata.normalize("NFKD", text.translate(MARKS)).encode("ascii", "ignore").decode()
    return text.replace("\\", "/").replace('"', "'")


class Texts(BaseModel):
    """One language's words for the module (docs/modules/example-module.yaml)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    tagline: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    items: str = Field(min_length=1, max_length=30)
    add: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=40)
    empty: str = Field(min_length=1, max_length=120)

    @field_validator("*", mode="before")
    @classmethod
    def one_line(cls, value: Any) -> Any:
        """Spaces as a page shows them: one between words, none around."""
        return " ".join(value.split()) if isinstance(value, str) else value


class ModuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Words of letters and digits joined by "_", each starting with a letter: so two keys never
    # make the same class name (foo_bar -> FooBar), and every name it makes fits a line.
    key: str = Field(min_length=2, max_length=24, pattern=r"^[a-z][a-z0-9]*(_[a-z][a-z0-9]*)*$")
    icon: str = Field(pattern=r"^[A-Z][A-Za-z0-9]*$")
    accent: Accent
    status: Literal["live", "beta"]
    order: int = Field(ge=0, le=10_000)
    keywords: tuple[str, ...] = ()
    strings: dict[Literal["en", "ta", "hi"], Texts]

    @field_validator("key")
    @classmethod
    def free_key(cls, key: str) -> str:
        if keyword.iskeyword(key):
            raise ValueError(f"{key} is a Python keyword: it can't name a package")
        if key.replace("_", "-") in RESERVED:
            raise ValueError(f"/v1/{key} belongs to the platform; choose another key")
        return key

    @model_validator(mode="after")
    def every_language_translated(self) -> ModuleSpec:
        missing = [language for language in LANGUAGES if language not in self.strings]
        if missing:
            raise ValueError(f"strings needs every language; missing: {', '.join(missing)}")
        english = self.strings["en"].model_dump()
        left = [
            f"{language}.{name}"
            for language in ("ta", "hi")
            for name, text in self.strings[language].model_dump().items()
            if text == english[name] and WORDY.search(text)
        ]
        if left:
            raise ValueError(f"still in English: {', '.join(left)}")
        return self

    @property
    def slug(self) -> str:
        return self.key.replace("_", "-")

    @property
    def camel(self) -> str:
        return "".join(part.capitalize() for part in self.key.split("_"))

    def tokens(self, package: str = PACKAGE) -> dict[str, str]:
        """What the templates' ``%%name%%`` tokens stand for; ``package`` holds the module."""
        english = self.strings["en"]
        return {
            "key": self.key,
            "module": f"{package}.{self.key}",
            "slug": self.slug,
            "Key": self.camel,
            "name": _in_docstring(english.name),
            "tagline": _in_docstring(english.tagline),
            "status": self.status.upper(),
            "status_value": self.status,
            "order": str(self.order),
        }


def load_spec(path: Path) -> ModuleSpec:
    try:
        # utf-8-sig: a file Windows saved with a byte-order mark reads the same.
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        return ModuleSpec.model_validate(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SpecError(f"{path} can't be read (save it as UTF-8): {exc}") from exc
    except ValidationError as exc:
        problems = [f"  {'.'.join(map(str, e['loc'])) or 'spec'}: {e['msg']}" for e in exc.errors()]
        raise SpecError(f"{path} isn't a module spec:\n" + "\n".join(problems)) from exc


def render(text: str, tokens: dict[str, str]) -> str:
    unknown = set(TOKEN.findall(text)) - set(tokens)
    if unknown:  # pragma: no cover - a template mistake, caught by the tests
        raise SpecError(f"a template has tokens nothing fills: {', '.join(sorted(unknown))}")
    return TOKEN.sub(lambda match: tokens[match.group(1)], text)


def backend_places(spec: ModuleSpec, root: Path, package: str = PACKAGE) -> dict[str, Path]:
    """Where the module (in ``package``) and its tests go."""
    return {
        "module": root.joinpath(*package.split("."), spec.key),
        "tests": root / "tests" / "modules" / spec.key,
    }


def backend_files(spec: ModuleSpec, root: Path, package: str = PACKAGE) -> dict[Path, str]:
    """Every backend file of the new module, by where it goes."""
    tokens = spec.tokens(package)
    files: dict[Path, str] = {}
    for folder, target in backend_places(spec, root, package).items():
        for template in sorted((TEMPLATES / folder).glob("*.tmpl")):
            name = template.name.removesuffix(".tmpl").replace("test_module", f"test_{spec.key}")
            files[target / name] = render(template.read_text(encoding="utf-8"), tokens)
    return files


def module_folders(spec: ModuleSpec, root: Path, frontend: Path | None) -> list[Path]:
    """Where the module goes; none of them may exist yet, nor a test module of that key."""
    folders = list(backend_places(spec, root).values())
    if frontend is not None:
        if not (frontend / "scripts" / "new-module.ts").is_file():
            raise SpecError(f"{frontend} is not the frontend repository (use --frontend)")
        folders.append(frontend / "src" / "modules" / spec.slug)
    fixture = root / "tests" / "fixtures" / "modules" / spec.key  # e.g. notes, in every test app
    taken = [str(folder) for folder in (*folders, fixture) if folder.exists()]
    if taken:
        raise SpecError(f"a module named {spec.key} is there already: {', '.join(taken)}")
    return folders


def write_backend(spec: ModuleSpec, root: Path, package: str = PACKAGE) -> None:
    """The backend files, in the project's own style: imports sorted, formatted, then held to
    the project's whole lint (a file that fails it is not kept)."""
    print("writing the backend module ...", flush=True)
    for path, text in backend_files(spec, root, package).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")  # LF on Windows too
    folders = [str(folder) for folder in backend_places(spec, root, package).values()]
    style = ["--config", str(PROJECT_ROOT / "pyproject.toml")]
    ruff = [sys.executable, "-m", "ruff"]
    run([*ruff, "check", "--fix", "--select", "I", *style, *folders], root)
    run([*ruff, "format", *style, *folders], root)
    if package == PACKAGE:  # in place, so the project's per-folder rules apply
        run([*ruff, "check", *style, *folders], root)


def run(
    command: Sequence[str], cwd: Path, env: dict[str, str] | None = None, what: str | None = None
) -> None:
    """Run a step; its output shows only if it fails, under ``what`` (or the command)."""
    done = subprocess.run(  # noqa: S603 - the project's own tools, no shell
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        step = what or " ".join(command[:4])
        raise SpecError(f"{step} failed:\n{done.stdout}{done.stderr}".rstrip())


def node() -> str:
    found = shutil.which("node")
    if found is None:
        raise SpecError("Node (24) must be on PATH: the frontend's generator needs it")
    return found


def export_schema(root: Path) -> None:
    """The API's schema with the new routes, for the frontend's types. No database is used, so
    stand-in settings do when there's no .env."""
    print("exporting the API's schema ...", flush=True)
    env = {**os.environ, "APP_ENV": "development"}  # noqa: TID251 - for a child process only
    env.setdefault("MONGO_URI", "mongodb://localhost:27017/openapi-export")
    env.setdefault("AUTH_JWT_SECRET", "openapi-export-" + "x" * 32)
    run([sys.executable, "scripts/export_openapi.py", str(root / SCHEMA)], root, env)


def frontend_generator(
    spec: ModuleSpec, frontend: Path, *options: str, what: str | None = None
) -> None:
    """The frontend's own generator (scripts/new-module.ts), given the spec as JSON."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{spec.key}.json"
        path.write_text(json.dumps(spec.model_dump(mode="json"), ensure_ascii=False), "utf-8")
        run([node(), "scripts/new-module.ts", "--spec", str(path), *options], frontend, what=what)


def frontend_check(spec: ModuleSpec, source: Path, frontend: Path, *, required: bool) -> None:
    """The frontend generator's check of the spec (the icon and colour too), before anything is
    written. Writing only the backend, it runs if the frontend is there to run it."""
    ready = shutil.which("node") and (frontend / "node_modules").is_dir()
    if not (required or (ready and (frontend / "scripts" / "new-module.ts").is_file())):
        print("note: the icon and colour are checked when the frontend is made", flush=True)
        return
    frontend_generator(spec, frontend, "--check", what=f"the frontend's check of {source}")


def write_frontend(spec: ModuleSpec, frontend: Path, root: Path) -> None:
    print("writing the frontend module, and its API types ...", flush=True)
    frontend_generator(spec, frontend)
    # The schema just exported, whatever OPENAPI_SOURCE the shell has.
    source = {**os.environ, "OPENAPI_SOURCE": str(root / SCHEMA)}  # noqa: TID251 - child only
    run([node(), "scripts/gen-api-types.mjs"], frontend, source)
    run(
        [node(), "node_modules/prettier/bin/prettier.cjs", "--write", "src/core/api/schema.d.ts"],
        frontend,
    )


def check(spec: ModuleSpec, root: Path, frontend: Path | None) -> list[str]:
    """The new module's tests and the shared checks it joins; what failed, if anything."""
    steps: list[tuple[str, list[str], Path]] = [
        (
            "backend tests",
            [sys.executable, "-m", "pytest", f"tests/modules/{spec.key}", *BACKEND_CHECKS, "-q"],
            root,
        )
    ]
    if frontend is not None:
        steps += [
            ("frontend types", [node(), "node_modules/typescript/bin/tsc", "-b"], frontend),
            (
                "frontend lint",
                [node(), "node_modules/eslint/bin/eslint.js", f"src/modules/{spec.slug}"],
                frontend,
            ),
            (
                "frontend tests",
                [
                    node(),
                    "node_modules/vitest/vitest.mjs",
                    "run",
                    f"src/modules/{spec.slug}",
                    *FRONTEND_CHECKS,
                ],
                frontend,
            ),
        ]
    failed = []
    for name, command, cwd in steps:
        print(f"checking: {name} ...", flush=True)
        try:
            run(command, cwd)
        except SpecError as exc:
            print(exc)
            failed.append(name)
    return failed


def undo(folders: Sequence[Path], saved: dict[Path, bytes | None]) -> list[str]:
    """Remove the module's folders (none existed before: module_folders made sure) and put the
    generated API files back. What couldn't be undone, if anything."""
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
    for path, content in saved.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    return [str(folder) for folder in folders if folder.exists()]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--spec", type=Path, required=True, help="The module's spec (YAML)")
    parser.add_argument("--frontend", type=Path, default=FRONTEND, help="The frontend repository")
    parser.add_argument("--backend-only", action="store_true", help="Write only the backend")
    parser.add_argument("--no-check", action="store_true", help="Skip the tests at the end")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, root: Path = PROJECT_ROOT) -> int:
    args = parse_args(argv)
    frontend = None if args.backend_only else args.frontend.resolve()
    try:
        spec = load_spec(args.spec)
        folders = module_folders(spec, root, frontend)
        frontend_check(spec, args.spec, args.frontend.resolve(), required=frontend is not None)
    except SpecError as exc:
        print(f"error: {exc}")
        return 1
    generated = [root / SCHEMA] + ([frontend / "src/core/api/schema.d.ts"] if frontend else [])
    saved = {path: path.read_bytes() if path.exists() else None for path in generated}
    try:
        write_backend(spec, root)
        if frontend is not None:
            export_schema(root)
            write_frontend(spec, frontend, root)
    except BaseException as exc:  # Ctrl+C too: never leave half a module behind
        left = undo(folders, saved)
        print(f"error: {exc}" if isinstance(exc, SpecError | OSError) else "stopped.")
        print(f"Couldn't remove: {', '.join(left)}" if left else "What it wrote is removed.")
        if not isinstance(exc, SpecError | OSError):
            raise
        return 1
    print(f"Made the {spec.key} module:")
    for folder in folders:
        print(f"  {folder}")
    failed = [] if args.no_check else check(spec, root, frontend)
    if failed:
        print(f"These checks failed: {', '.join(failed)}. The files are kept, to look into.")
        return 1
    print("Next: read the new files and commit them; grow the module from there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
