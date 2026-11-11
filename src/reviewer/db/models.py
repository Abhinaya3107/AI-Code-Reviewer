"""PostgreSQL schema. Diffs and source code are deliberately NOT stored: only review results."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so migrations and models always agree.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class ReviewStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


_now = func.now()


class Installation(Base):
    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("github_installation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_installation_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now)


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("github_repo_id"),
        Index("ix_repositories_installation_id", "installation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="CASCADE"))
    github_repo_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(255))
    # Dashboard toggle. When false the worker skips reviews for this repository.
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="1", default=True)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now)


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_pull_requests_repository_id_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), server_default="open", default="open")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # One review per commit: makes retries and redeliveries idempotent.
        UniqueConstraint("pull_request_id", "head_sha", name="uq_reviews_pull_request_id_head_sha"),
        Index("ix_reviews_pull_request_id_created_at", "pull_request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pull_request_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id", ondelete="CASCADE"))
    head_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(
            ReviewStatus,
            name="review_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ReviewStatus.queued,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    overall_risk: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files_reviewed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewComment(Base):
    __tablename__ = "review_comments"
    __table_args__ = (Index("ix_review_comments_review_id", "review_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(500))
    line: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Hash of (path, message): identifies the same finding across commits.
    fingerprint: Mapped[str] = mapped_column(String(64))
    # +1 / -1 from a future UI, null until rated.
    feedback: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_events_installation_id_month", "installation_id", "month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="CASCADE"))
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("reviews.id", ondelete="SET NULL"), nullable=True
    )
    # "YYYY-MM", for monthly usage caps and analytics.
    month: Mapped[str] = mapped_column(String(7))
    tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now)
