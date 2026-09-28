from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from vey.domain import utcnow


def uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "vey_core"}
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[str] = mapped_column(String(64), default=uid)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict] = mapped_column(JSONB, default=dict)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_queue", "status", "created_at"), {"schema": "vey_core"})
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    message_id: Mapped[str] = mapped_column(String(160), unique=True)
    user_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(16), default="debug")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = {"schema": "vey_core"}
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("vey_core.tasks.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("task_id", "kind"), {"schema": "vey_core"})
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("vey_core.tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="result")
    user_id: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_parts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecutorSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "vey_exec"}
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Grant(Base):
    __tablename__ = "grants"
    __table_args__ = {"schema": "vey_exec"}
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(16))
    container_ids: Mapped[list] = mapped_column(JSONB)
    policy_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LogCursor(Base):
    __tablename__ = "log_cursors"
    __table_args__ = {"schema": "vey_exec"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    generation: Mapped[str] = mapped_column(String(64))
    container_ids: Mapped[list] = mapped_column(JSONB)
    target: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def database(url: str):
    engine = create_engine(
        url, pool_pre_ping=True, pool_size=4, max_overflow=0, connect_args={"connect_timeout": 5}
    )
    return engine, sessionmaker(engine, expire_on_commit=False)


def db_now(db) -> datetime:
    return db.scalar(func.now())
