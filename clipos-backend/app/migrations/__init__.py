"""Ordered, transactional additive migrations for the local SQLite library."""
from sqlalchemy import inspect, text


def migrate(engine):
    from app.models.job import Job
    from app.models.execution import PipelineRun, StageAttempt, ClipState
    from app.models.edit import SourceAsset, ClipEdit, EditRevision, RenderOutput
    from app.models.source_import import SourceImport
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations '
                          '(version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'))
        # Serializes concurrent API/worker startup before checking migration versions.
        if engine.dialect.name == 'sqlite':
            conn.execute(text('UPDATE schema_migrations SET version=version WHERE version=0'))
        versions = set(conn.execute(text('SELECT version FROM schema_migrations')).scalars())
        if 1 not in versions:
            Job.__table__.create(conn, checkfirst=True)
            columns = {c['name'] for c in inspect(conn).get_columns('jobs')}
            if 'updated_at' not in columns:
                conn.execute(text('ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP'))
                conn.execute(text('UPDATE jobs SET updated_at=created_at'))
            if 'error_message' not in columns:
                conn.execute(text('ALTER TABLE jobs ADD COLUMN error_message TEXT'))
            conn.execute(text('INSERT INTO schema_migrations(version) VALUES (1)'))
        if 2 not in versions:
            for model in (PipelineRun, StageAttempt, ClipState):
                model.__table__.create(conn, checkfirst=True)
            conn.execute(text('INSERT INTO schema_migrations(version) VALUES (2)'))

        if 3 not in versions:
            for model in (SourceAsset, ClipEdit, EditRevision, RenderOutput):
                model.__table__.create(conn, checkfirst=True)
            conn.execute(text('INSERT INTO schema_migrations(version) VALUES (3)'))
        if 4 not in versions:
            SourceImport.__table__.create(conn, checkfirst=True)
            conn.execute(text('INSERT INTO schema_migrations(version) VALUES (4)'))
        if 5 not in versions:
            columns = {c['name'] for c in inspect(conn).get_columns('clip_edits')}
            if 'generated_signature' not in columns:
                conn.execute(text('ALTER TABLE clip_edits ADD COLUMN generated_signature TEXT'))
            conn.execute(text('INSERT INTO schema_migrations(version) VALUES (5)'))
