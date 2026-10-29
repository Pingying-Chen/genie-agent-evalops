"""Load an LLM skill: `skills/<name>/SKILL.md` = YAML front matter + a prompt template.

No prompt strings live in Python. A skill is versioned in its front matter; every result
records `name@version` and the model, so calibration is per (skill version, model).
"""

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from evalops.errors import ConfigError
from evalops.schemas.llm import SCHEMAS

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_REPO_SKILLS = Path(__file__).resolve().parents[4] / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    purpose: str
    template: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    model_params: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None  # a space's model for this skill; None = the deployment's
    variant: str = ""  # a space's prompt or sampling override, e.g. "s2"

    @property
    def ref(self) -> str:
        """`name@version`, or `name@version~variant` for a space's override: calibration and the
        manifests keep the two apart."""
        return f"{self.name}@{self.version}" + (f"~{self.variant}" if self.variant else "")

    def render(self, data: BaseModel) -> str:
        """Fill `{field}` placeholders for the input's fields only.

        Not `str.format`: prompts contain literal JSON braces, and inputs (reports, SQL) may too.
        """
        if not isinstance(data, self.input_schema):
            raise TypeError(f"{self.ref} expects {self.input_schema.__name__}")
        values = {k: str(v) for k, v in data.model_dump().items()}
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), self.template)


def skills_dir(configured: str = "") -> Path:
    if configured:
        return Path(configured)
    packaged = resources.files("evalops") / "skills_default"
    return Path(str(packaged)) if packaged.is_dir() else _REPO_SKILLS


def load_skill(name: str, folder: Path) -> Skill:
    path = folder / name / "SKILL.md"
    if not path.exists():
        raise ConfigError(f"Skill {name!r} not found at {path}.")
    text = path.read_text()
    if not text.startswith("---"):
        raise ConfigError(f"{path}: missing YAML front matter.")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    try:
        return Skill(
            name=meta["name"],
            version=int(meta["version"]),
            purpose=meta.get("purpose", ""),
            template=body.strip() + "\n",
            input_schema=SCHEMAS[meta["input_schema"]],
            output_schema=SCHEMAS[meta["output_schema"]],
            model_params=dict(meta.get("model") or {}),
        )
    except KeyError as exc:
        raise ConfigError(f"{path}: invalid front matter ({exc}).") from exc
