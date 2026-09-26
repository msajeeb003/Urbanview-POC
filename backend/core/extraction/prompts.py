"""Versioned prompt sets of AI document extraction.

A prompt set is a directory ``prompt_sets/v<version>/``: ``manifest.toml`` (the prompt version, the
schema version its responses are validated into, one entry per task), Jinja2 templates for the
system prompt (extraction rules + planning glossary), the field guide, the per-task instructions,
the document context and the user message, and ``responses/``: the structured-output schemas the
set sends, exactly as sent (generated from ``core.extraction.response``; a test keeps the current
set's files in step). Every extraction item records the prompt version that produced it.

The templates carry no place-specific words: the municipality, the documents' language, the
planning glossary and the document types come from the municipality profile (``[terminology]``
and ``[extraction]`` in ``municipalities/<id>.toml``).

A request is two cached system blocks (rules + glossary, stable per municipality; the task, field
guide and document, stable per document) and one user message with the pages.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import jinja2
from pydantic import BaseModel

from core.extraction.pages import PageInput, render_pages
from core.extraction.response import RESPONSE_MODELS, strict_json_schema
from core.extraction.schema import SCHEMA_VERSION, TaskKind
from core.municipality import load_extraction_profile, load_profile

PROMPT_VERSION = "1.1"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompt_sets"


@dataclass(frozen=True, slots=True)
class DocumentContext:
    """The document version being read (``planning_documents``)."""

    id: int
    name: str
    type: str | None = None


@dataclass(frozen=True, slots=True)
class SystemBlock:
    text: str
    cache: bool = False


@dataclass(frozen=True, slots=True)
class PromptBundle:
    task: TaskKind
    prompt_version: str
    schema_version: str
    system: tuple[SystemBlock, ...]
    user: str
    schema: dict[str, Any]
    response_name: str


@dataclass(frozen=True, slots=True)
class TaskPrompt:
    instructions: str
    response: str
    fields: bool


@dataclass(frozen=True, slots=True)
class PromptSet:
    version: str
    schema_version: str
    directory: Path
    system: str
    fields: str
    context: str
    user: str
    tasks: dict[str, TaskPrompt]

    def environment(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.directory),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )

    def response_schema(self, response: str) -> dict[str, Any]:
        """The structured-output schema this set sends for ``response``."""
        path = self.directory / "responses" / f"{response}.schema.json"
        return json.loads(path.read_text(encoding="utf-8"))


def prompt_dir(version: str) -> Path:
    return PROMPTS_DIR / f"v{version}"


@cache
def load_prompt_set(version: str = PROMPT_VERSION) -> PromptSet:
    directory = prompt_dir(version)
    with (directory / "manifest.toml").open("rb") as fh:
        manifest = tomllib.load(fh)
    if manifest["prompt_version"] != version:
        raise ValueError(f"{directory} declares prompt_version {manifest['prompt_version']}")
    return PromptSet(
        version=version,
        schema_version=manifest["schema_version"],
        directory=directory,
        system=manifest["system"],
        fields=manifest["fields"],
        context=manifest["context"],
        user=manifest["user"],
        tasks={
            name: TaskPrompt(t["instructions"], t["response"], bool(t.get("fields", False)))
            for name, t in manifest["tasks"].items()
        },
    )


def response_model(name: str) -> type[BaseModel]:
    """The response model a prompt set names: 1.0's nested models or the compact ones."""
    from core.extraction.compact import COMPACT_MODELS

    models = {model.__name__: model for model in RESPONSE_MODELS.values()} | COMPACT_MODELS
    return models[name]


def generated_response_schemas(version: str = PROMPT_VERSION) -> dict[str, dict[str, Any]]:
    """Response name -> structured-output schema of the responses a prompt set uses."""
    names = {task.response for task in load_prompt_set(version).tasks.values()}
    return {name: strict_json_schema(response_model(name)) for name in sorted(names)}


def write_response_schemas(version: str = PROMPT_VERSION) -> list[Path]:
    """Regenerate ``responses/*.schema.json`` of the current prompt set."""
    if version != PROMPT_VERSION:
        raise ValueError("only the current prompt set is generated; older sets are history")
    target = prompt_dir(version) / "responses"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in sorted(generated_response_schemas(version).items()):
        path = target / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def build_prompt(
    task: TaskKind,
    *,
    municipality_id: str,
    document: DocumentContext,
    pages: Sequence[PageInput],
    version: str = PROMPT_VERSION,
) -> PromptBundle:
    """The system blocks, user message and response schema of one extraction request."""
    prompt_set = load_prompt_set(version)
    if prompt_set.schema_version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ValueError(
            f"prompt set {version} targets schema {prompt_set.schema_version}, "
            f"this code reads {SCHEMA_VERSION}"
        )
    spec = prompt_set.tasks.get(task)
    if spec is None:
        raise ValueError(f"prompt set {version} has no task {task!r}")
    profile = load_profile(municipality_id)
    extraction = load_extraction_profile(municipality_id)
    if extraction is None:
        raise LookupError(f"municipality '{municipality_id}' has no [extraction] profile")
    env = prompt_set.environment()
    values = {
        "municipality": profile.name,
        "language": extraction.language,
        "glossary": extraction.glossary,
        "document_types": profile.terminology.document_types,
        "document_type_codes": ", ".join(profile.terminology.document_types),
        "document": document,
    }
    rules = env.get_template(prompt_set.system).render(values).strip()
    parts = [env.get_template(spec.instructions).render(values).strip()]
    if spec.fields:
        parts.append(env.get_template(prompt_set.fields).render(values).strip())
    parts.append(env.get_template(prompt_set.context).render(values).strip())
    user = env.get_template(prompt_set.user).render(pages=render_pages(pages)).strip()
    return PromptBundle(
        task=task,
        prompt_version=prompt_set.version,
        schema_version=prompt_set.schema_version,
        system=(SystemBlock(rules, cache=True), SystemBlock("\n\n".join(parts), cache=True)),
        user=user,
        schema=prompt_set.response_schema(spec.response),
        response_name=spec.response,
    )
