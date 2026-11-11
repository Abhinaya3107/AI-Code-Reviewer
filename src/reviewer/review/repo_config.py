"""Per-repository settings, read from .aireview.yml on the repository's base branch."""

from typing import Annotated

import yaml
from pydantic import BaseModel, Field, ValidationError

from reviewer.review.schema import Category, Severity

CONFIG_PATH = ".aireview.yml"


class RepoConfig(BaseModel):
    enabled: bool = True
    # Lowest severity to post: "warning" hides suggestions and nitpicks.
    severity_threshold: Severity = "nitpick"
    max_comments: Annotated[int, Field(ge=1, le=30)] = 15
    ignore_paths: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=50)
    ] = []
    focus: list[Category] = []
    custom_instructions: Annotated[str, Field(max_length=2000)] = ""


DEFAULT_CONFIG = RepoConfig()


def parse_repo_config(text: str) -> tuple[RepoConfig, str | None]:
    """Parses and validates the file. An invalid file falls back to defaults and reports why."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).strip().splitlines()[0] if str(err).strip() else "parse error"
        return DEFAULT_CONFIG, f"invalid YAML ({first_line})"

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return DEFAULT_CONFIG, "file: expected a mapping of settings"

    try:
        return RepoConfig.model_validate(raw), None
    except ValidationError as err:
        issue = err.errors()[0]
        where = ".".join(str(p) for p in issue["loc"]) or "file"
        return DEFAULT_CONFIG, f"{where}: {issue['msg']}"
