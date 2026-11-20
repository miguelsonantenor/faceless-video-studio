"""The core service: owns config, database, events, activity, settings and jobs.

``Core.open()`` does first-start setup (config, token, folders, database, defaults);
``start()`` recovers interrupted jobs and starts the workers; ``stop()`` shuts down.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from fvs.activity import ActivityLog
from fvs.analyze.analyzer import Analyzer
from fvs.analyze.service import AnalysisService
from fvs.analyze.transcribe import Whisper
from fvs.assets import AssetService
from fvs.batch import BatchService
from fvs.config import Config, ensure_config, ensure_token, fvs_home
from fvs.events import EventBus
from fvs.export.exporter import Exporter
from fvs.jobs import JobQueue
from fvs.library import Library
from fvs.logs import configure_logging, get_logger, register_secret
from fvs.pathguard import PathGuard
from fvs.plan.planner import Planner
from fvs.projects import Projects
from fvs.render.service import RenderService, install_fonts
from fvs.review.service import ReviewService
from fvs.script.service import ScriptService
from fvs.see.service import Perception
from fvs.see.transcripts import Transcripts
from fvs.settings import SettingsService, effective_renders_at_once
from fvs.spec.service import SpecService
from fvs.spec.templates import Templates
from fvs.store import Database
from fvs.voice.budget import Budget
from fvs.voice.elevenlabs import ElevenLabs
from fvs.voice.profiles import VoiceProfiles
from fvs.voice.service import VoiceService

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
    projects: Projects
    profiles: VoiceProfiles
    scripts: ScriptService
    budget: Budget
    voice: VoiceService
    templates: Templates
    specs: SpecService
    planner: Planner
    renders: RenderService
    perception: Perception
    reviews: ReviewService
    batches: BatchService
    assets: AssetService

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
        projects = Projects(db, library, activity, folders.workspace)
        profiles = VoiceProfiles(db, activity)
        profiles.seed()
        scripts = ScriptService(db, projects, profiles, settings, activity)
        budget = Budget(db, settings)

        def api_key() -> str | None:  # read on every call: Settings may replace the key
            key = config.elevenlabs_api_key
            return key.get_secret_value() if key is not None else None

        voice = VoiceService(
            db, projects=projects, scripts=scripts, profiles=profiles,
            client=ElevenLabs(api_key), budget=budget, jobs=jobs, settings=settings,
            activity=activity, workspace=folders.workspace)
        templates = Templates(db)
        templates.seed()
        specs = SpecService(db, projects=projects, scripts=scripts, voice=voice,
                            templates=templates, activity=activity)
        planner = Planner(projects=projects, voice=voice, specs=specs, templates=templates,
                          analyzer=analyzer, settings=settings)
        fonts_dir = folders.assets / "fonts"
        install_fonts(Path(str(resources.files("fvs") / "assets_bundled" / "fonts")),
                      fonts_dir)
        transcripts = Transcripts(whisper, folders.workspace / "cache" / "transcripts")
        renders = RenderService(db, projects=projects, scripts=scripts, specs=specs,
                                templates=templates, settings=settings, jobs=jobs,
                                activity=activity, transcripts=transcripts,
                                fonts_dir=fonts_dir)
        perception = Perception(db, library=library, projects=projects, scripts=scripts,
                                specs=specs, renders=renders, settings=settings,
                                transcripts=transcripts, jobs=jobs,
                                workspace=folders.workspace)
        exporter = Exporter(projects=projects, scripts=scripts, specs=specs, voice=voice,
                            output=folders.output)
        reviews = ReviewService(db, projects=projects, specs=specs, exporter=exporter,
                                activity=activity)
        batches = BatchService(db, projects=projects, voice=voice, planner=planner,
                               specs=specs, renders=renders, jobs=jobs, budget=budget,
                               activity=activity)
        assets = AssetService(db, guard, activity, folders.assets)
        return cls(home=home, config=config, token=token, db=db, bus=bus, activity=activity,
                   settings=settings, jobs=jobs, guard=guard, library=library,
                   whisper=whisper, analyzer=analyzer, analysis=analysis,
                   projects=projects, profiles=profiles, scripts=scripts, budget=budget,
                   voice=voice, templates=templates, specs=specs, planner=planner,
                   renders=renders, perception=perception, reviews=reviews,
                   batches=batches, assets=assets)

    def start(self) -> None:
        self.jobs.recover()
        self.jobs.start()
        log.info("core_started", port=self.config.port, workers=self.jobs.n_workers)

    def stop(self) -> None:
        self.jobs.stop()
        log.info("core_stopped")
