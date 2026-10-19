from datetime import datetime, timezone
from sqlalchemy import (MetaData, Table, Column, String, Integer, Float, DateTime,
                        ForeignKey, UniqueConstraint, JSON)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
document = JSON().with_variant(JSONB(), "postgresql")


def timestamp():
    return Column("created_at", DateTime(timezone=True), nullable=False,
                  default=lambda: datetime.now(timezone.utc))


projects = Table("projects", metadata,
    Column("id", String(32), primary_key=True),
    Column("repo_path", String, nullable=False, unique=True), timestamp())
runs = Table("runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("project_id", ForeignKey("projects.id"), nullable=False, index=True),
    Column("status", String(40), nullable=False),
    Column("task_branch", String, nullable=False),
    Column("settings", document, nullable=False),
    Column("artifact_directory", String, nullable=False), timestamp())
checkpoints = Table("checkpoints", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("state", document, nullable=False),
    Column("git_head", String(64), nullable=False),
    Column("source_fingerprint", String(64), nullable=False),
    Column("working_tree_fingerprint", String(64), nullable=False),
    Column("phase", String(40), nullable=False),
    Column("step", Integer, nullable=False),
    Column("provider", String, nullable=False),
    Column("model", String, nullable=False), timestamp(),
    UniqueConstraint("run_id", "sequence"))
model_calls = Table("model_calls", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("step", Integer, nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("provider", String, nullable=False),
    Column("model", String, nullable=False),
    Column("cached_input", Integer), Column("uncached_input", Integer),
    Column("input_tokens", Integer), Column("output_tokens", Integer),
    Column("raw_usage", document), Column("latency", Float, nullable=False),
    Column("status", String(32), nullable=False), timestamp())
