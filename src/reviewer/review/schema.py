"""The shape of a review, shared by the LLM output validator, the database layer and the GitHub poster."""

from typing import Literal, get_args

from pydantic import BaseModel, Field

Severity = Literal["critical", "warning", "suggestion", "nitpick"]
Category = Literal["bug", "security", "performance", "maintainability", "testing"]
Risk = Literal["low", "medium", "high"]

# Ordered most to least severe: the index doubles as a ranking.
SEVERITIES: tuple[str, ...] = get_args(Severity)
CATEGORIES: tuple[str, ...] = get_args(Category)
RISKS: tuple[str, ...] = get_args(Risk)


class ReviewComment(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    severity: Severity
    category: Category
    message: str = Field(min_length=1)
    suggested_fix: str | None = None


class ReviewResult(BaseModel):
    summary: str = Field(min_length=1)
    overall_risk: Risk
    comments: list[ReviewComment]
