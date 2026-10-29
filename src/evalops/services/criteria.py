"""Load criteria from YAML and version them by content.

Criteria live in `criteria/*.yaml` (one file per criterion). The wheel ships them as
`evalops/criteria_default`: the generic set, which fits any Genie Agent.
`EVALOPS_CRITERIA_DIR` replaces that base folder; `EVALOPS_EXTRA_CRITERIA_DIR` layers a
deployment's own criteria on top (rules about its data, judges of its own instructions), each
replacing a base criterion of the same id. The demo's are in `demo/criteria`.

The version is a hash of the files' names and contents, so it works without git (jobs run
from a wheel) and changes exactly when a criterion changes.
"""

import hashlib
from importlib import resources
from pathlib import Path

import yaml

from evalops.errors import ConfigError
from evalops.schemas.checks import Criterion

_REPO_CRITERIA = Path(__file__).resolve().parents[3] / "criteria"


def criteria_dir(configured: str = "") -> Path:
    """Configured folder, else the packaged defaults, else the repo folder (dev checkout)."""
    if configured:
        return Path(configured)
    packaged = resources.files("evalops") / "criteria_default"
    if packaged.is_dir():
        return Path(str(packaged))
    return _REPO_CRITERIA


def criteria_folders(base: str = "", extra: str = "", root: str = "") -> list[Path]:
    """The base folder, then the extra one if set. A relative `extra` resolves against `root`
    (the jobs pass the bundle's files folder) or else the working directory (the app runs from
    its source folder). `none` means no extra folder: Databricks Apps reject empty env values."""
    folders = [criteria_dir(base)]
    if extra and extra.lower() != "none":
        path = Path(extra)
        folders.append(path if path.is_absolute() or not root else Path(root) / path)
    return folders


def load_criteria(*folders: Path) -> tuple[list[Criterion], str]:
    """All criteria in `folders`, sorted by id, and their content-hash version. A later folder's
    criterion replaces an earlier one with the same id.

    Raises `ConfigError` on an invalid file, naming it, so a bad edit fails loudly, and on a
    missing extra folder, so a typo in the setting does not silently drop a space's rules.
    """
    digest = hashlib.sha256()
    by_id: dict[str, Criterion] = {}
    for position, folder in enumerate(folders):
        if not folder.is_dir():
            raise ConfigError(f"Criteria folder {folder} does not exist.")
        for file in sorted(folder.glob("*.yaml")):
            text = file.read_text()
            digest.update(f"{position}/{file.name}".encode())
            digest.update(text.encode())
            try:
                criterion = Criterion.model_validate(yaml.safe_load(text))
            except Exception as exc:
                raise ConfigError(f"Invalid criterion file {file.name}: {exc}") from exc
            if criterion.id != file.stem:
                raise ConfigError(f"{file.name}: id {criterion.id!r} must match the file name.")
            by_id[criterion.id] = criterion
    if not by_id:
        raise ConfigError(f"No criteria found in {', '.join(map(str, folders))}.")
    return sorted(by_id.values(), key=lambda c: c.id), f"sha256:{digest.hexdigest()[:12]}"
