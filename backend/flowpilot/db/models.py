"""Persistence model (PostgreSQL in deployment, SQLite for zero-setup local runs).

runs             one live session or benchmark run (mode, scenario, scheduler, summary)
samples          per-second telemetry of a run (apps, classes, capacity, shaper) as JSON
decisions        the controller's explainable decision log
flows            classified flows (label, confidence, class, explanation, rates)
device_profiles  what FlowPilot has learned about each device over time
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="live")  # live | benchmark
    mode: Mapped[str] = mapped_column(String(16))  # sim | kernel
    scenario: Mapped[str] = mapped_column(String(64))
    scheduler: Mapped[str] = mapped_column(String(32), index=True)
    seed: Mapped[int] = mapped_column(Integer, default=1)
    intent: Mapped[str] = mapped_column(String(32), default="balanced")
    status: Mapped[str] = mapped_column(String(16), default="running")
    label: Mapped[str] = mapped_column(String(128), default="")
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    samples: Mapped[list["Sample"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Sample(Base):
    __tablename__ = "samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    side: Mapped[str] = mapped_column(String(4), default="a")
    t: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JsonType)
    run: Mapped[Run] = relationship(back_populates="samples")


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    t: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JsonType, nullable=True)


class FlowRecord(Base):
    __tablename__ = "flows"
    __table_args__ = (UniqueConstraint("run_id", "flow_key", name="uq_flow_run_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    flow_key: Mapped[str] = mapped_column(String(128))
    device: Mapped[str] = mapped_column(String(64), index=True)
    proto: Mapped[str] = mapped_column(String(8))
    wan_ip: Mapped[str] = mapped_column(String(64))
    wan_port: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    tc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kbps_up: Mapped[float | None] = mapped_column(Float, nullable=True)
    kbps_down: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceProfile(Base):
    __tablename__ = "device_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device: Mapped[str] = mapped_column(String(64), unique=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(64), default="")
    profile: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
