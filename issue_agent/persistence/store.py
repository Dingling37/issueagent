import os
from contextlib import contextmanager
from uuid import uuid4
from sqlalchemy import create_engine, select, insert, update, func
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from . import PersistenceError
from .status import canonical_status, validate_transition
from .schema import projects, runs, checkpoints, model_calls


class Store:
    def __init__(self, engine):
        self.engine = engine

    @classmethod
    def from_environment(cls):
        value = os.environ.get("ISSUE_AGENT_DATABASE_URL")
        if not value:
            return None  # Explicitly opt in; existing Benchmark/local runs stay unchanged.
        try:
            url = make_url(value)
            if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                raise ValueError()
            return cls(create_engine(url.set(drivername="postgresql+psycopg"),
                                    pool_pre_ping=True, connect_args={"connect_timeout": 10}))
        except Exception:
            raise PersistenceError("Invalid PostgreSQL configuration (ISSUE_AGENT_DATABASE_URL)") from None

    @contextmanager
    def transaction(self):
        try:
            with self.engine.begin() as connection:
                yield connection
        except SQLAlchemyError:
            raise PersistenceError("PostgreSQL persistence failed; check database availability and Alembic migration. Run stopped.") from None

    def check(self):
        with self.transaction() as connection:
            for table in (projects, runs, checkpoints, model_calls):
                connection.execute(select(table).limit(0))

    def create_run(self, agent, settings):
        run_id = agent.tracer.directory.name
        with self.transaction() as connection:
            root = str(agent.workspace.root)
            project = connection.scalar(select(projects.c.id).where(projects.c.repo_path == root))
            if project is None:
                project = uuid4().hex
                connection.execute(insert(projects).values(id=project, repo_path=root))
            connection.execute(insert(runs).values(id=run_id, project_id=project, status="RUNNING",
                task_branch=agent.state.git_task["task_branch"], settings=settings,
                artifact_directory=agent.tracer.relative))
        return run_id

    def checkpoint(self, run_id, state, phase, identity, provider, model):
        with self.transaction() as connection:
            # Serialize concurrent sequence assignment on the parent run.
            previous = connection.execute(select(runs.c.status).where(runs.c.id == run_id).with_for_update()).scalar_one()
            validate_transition(previous, state.status)
            sequence = (connection.scalar(select(func.max(checkpoints.c.sequence)).where(
                checkpoints.c.run_id == run_id)) or 0) + 1
            connection.execute(insert(checkpoints).values(run_id=run_id, sequence=sequence,
                state=state.to_dict(), phase=phase, step=state.step_count,
                provider=provider, model=model, **identity))
            connection.execute(update(runs).where(runs.c.id == run_id).values(status=canonical_status(state.status)))

    def model_call(self, run_id, **values):
        with self.transaction() as connection:
            connection.execute(insert(model_calls).values(run_id=run_id, **values))

    def set_status(self, run_id, status):
        with self.transaction() as connection:
            previous = connection.execute(select(runs.c.status).where(runs.c.id == run_id).with_for_update()).scalar_one()
            validate_transition(previous, status)
            connection.execute(update(runs).where(runs.c.id == run_id).values(status=canonical_status(status)))

    def update_settings(self, run_id, settings):
        with self.transaction() as connection:
            connection.execute(update(runs).where(runs.c.id == run_id).values(settings=settings))

    def load(self, run_id):
        with self.transaction() as connection:
            run = connection.execute(select(runs, projects.c.repo_path).join(projects).where(
                runs.c.id == run_id)).mappings().first()
            checkpoint = connection.execute(select(checkpoints).where(checkpoints.c.run_id == run_id)
                .order_by(checkpoints.c.sequence.desc()).limit(1)).mappings().first()
            if run is None or checkpoint is None:
                raise PersistenceError("Run or complete checkpoint not found")
            return dict(run), dict(checkpoint)

    @contextmanager
    def lease(self, run_id):
        # Session advisory lock releases automatically after a crashed process disconnects.
        if self.engine.dialect.name != "postgresql":
            yield
            return
        import hashlib
        key = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big", signed=True)
        from sqlalchemy import text
        try:
            with self.engine.connect() as connection:
                if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                    from ..core.recovery import StateConflict
                    raise StateConflict("Run already active in another process")
                try:
                    yield
                finally:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        except SQLAlchemyError:
            raise PersistenceError("PostgreSQL runtime lock unavailable; run stopped") from None
