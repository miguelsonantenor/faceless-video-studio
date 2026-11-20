"""The core service: owns config, database, events, activity, settings and jobs.

``Core.open()`` does first-start setup (config, token, folders, database, defaults);
``start()`` recovers interrupted jobs and starts the workers; ``stop()`` shuts down.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fvs.activity import ActivityLog
from fvs.analyze.analyzer import Analyzer
from fvs.analyze.service import AnalysisService
from fvs.analyze.transcribe import Whisper
from fvs.config import Config, ensure_config, ensure_token, fvs_home
from fvs.events import EventBus
from fvs.jobs import JobQueue
from fvs.library import Library
from fvs.logs import configure_logging, get_logger, register_secret
from fvs.pathguard import PathGuard
from fvs.settings import SettingsService, effective_renders_at_once
from fvs.store import Database

log = get_logger("fvs.core")

WORKSPACE_DIRS = ("logs", "cache", "voice", "projects", "assets/music", "assets/fonts",
                  "assets/logos", "assets/images", "assets/sfx")


@dataclass
class Core:
    home: Path
    config: Config
    token: str
    db: Database
    bus: EventBus
    activity: ActivityLog
    settings: SettingsService
    jobs: JobQueue
    guard: PathGuard
    library: Library
    whisper: Whisper
    analyzer: Analyzer
    analysis: AnalysisService

    @classmethod
    def open(cls, home: Path | None = None, *, configure_logs: bool = True) -> Core:
        home = home or fvs_home()
        config = ensure_config(home)
        token = ensure_token(home)
        register_secret(token)
        if config.elevenlabs_api_key is not None:
            register_secret(config.elevenlabs_api_key.get_secret_value())

        folders = config.folders.expanded()
        for d in (folders.input, folders.workspace, folders.output):
            d.mkdir(parents=True, exist_ok=True)  # creating an empty input folder is fine
        for sub in WORKSPACE_DIRS:
            (folders.workspace / sub).mkdir(parents=True, exist_ok=True)
        if configure_logs:
            configure_logging(folders.workspace / "logs")

        db = Database(folders.workspace / "fvs.db")
        db.migrate()
        bus = EventBus()
        activity = ActivityLog(db, bus)
        settings = SettingsService(db, activity, bus)
        settings.seed()
        jobs = JobQueue(db, bus, activity,
                        workers=effective_renders_at_once(settings.get()),
                        workspace=folders.workspace)
        guard = PathGuard(folders)
        library = Library(db, guard, activity)
        whisper = Whisper(home / "models")
        analyzer = Analyzer(folders.workspace / "cache", whisper)
        analysis = AnalysisService(library, analyzer, jobs, activity)
        return cls(home=home, config=config, token=token, db=db, bus=bus, activity=activity,
                   settings=settings, jobs=jobs, guard=guard, library=library,
                   whisper=whisper, analyzer=analyzer, analysis=analysis)

    def start(self) -> None:
        self.jobs.recover()
        self.jobs.start()
        log.info("core_started", port=self.config.port, workers=self.jobs.n_workers)

    def stop(self) -> None:
        self.jobs.stop()
        log.info("core_stopped")
