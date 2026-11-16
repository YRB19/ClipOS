"""Explicit bounded derived-cache maintenance. Never deletes source or edit media."""
import shutil
from pathlib import Path
from sqlmodel import Session, select
from app.config import settings
from app.models.job import Job, engine
from app.models.execution import PipelineRun
from app.models.edit import RenderOutput


def cache_plan(db_engine=None):
    with Session(db_engine or engine) as session:
        protected = set(session.exec(select(PipelineRun.job_id).where(PipelineRun.active_key.is_not(None))).all())
        # Sources, edit revisions and render outputs are outside the removable folders.
        protected.update(session.exec(select(RenderOutput.job_id).where(RenderOutput.active_key.is_not(None))).all())
        retained_paths = [Path(path).resolve() for path in session.exec(select(Job.video_path)).all() if path]
        retained_paths.extend(Path(path).resolve() for path in session.exec(select(RenderOutput.output_path)).all() if path)
    root = Path(settings.storage_raw).resolve()
    entries = []
    for job in root.iterdir() if root.exists() else []:
        if job.is_symlink() or not job.is_dir():
            continue
        for name in ('chunks', 'assets'):
            folder = job/name
            if folder.is_symlink() or not folder.is_dir():
                continue
            files = [p for p in folder.rglob('*') if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)]
            entries.append(dict(job_id=job.name, kind=name, path=str(folder),
                                bytes=sum(p.stat().st_size for p in files),
                                mtime=max((p.stat().st_mtime for p in files), default=0),
                                protected=job.name in protected or any(p.is_relative_to(folder.resolve()) for p in retained_paths)))
    used = sum(e['bytes'] for e in entries)
    remaining, remove = used, []
    for entry in sorted(entries, key=lambda e: e['mtime']):
        if remaining <= settings.cache_max_gb * 1024**3:
            break
        if not entry['protected']:
            remove.append(entry)
            remaining -= entry['bytes']
    return dict(bytes=used, limit_bytes=int(settings.cache_max_gb*1024**3), remaining_bytes=remaining,
                protected_jobs=sorted(protected), remove=remove, over_limit=remaining > settings.cache_max_gb*1024**3)


def clean_cache(db_engine=None):
    # Hold SQLite's write lock while recomputing protection and deleting so a run
    # cannot acquire a source between the protected-job check and deletion.
    from sqlalchemy import text
    db_engine = db_engine or engine
    with db_engine.connect() as conn:
        conn.execute(text('BEGIN IMMEDIATE'))
        try:
            plan = cache_plan(db_engine)
            for entry in plan['remove']:
                shutil.rmtree(entry['path'])
            conn.commit()
            return plan
        except Exception:
            conn.rollback()
            raise


def ensure_cache_capacity(db_engine=None):
    plan = cache_plan(db_engine)
    if plan['bytes'] > plan['limit_bytes']:
        raise ValueError('Derived cache exceeds CACHE_MAX_GB; inspect /storage/cache and explicitly clean inactive cache or raise the limit')
