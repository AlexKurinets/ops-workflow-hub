from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from hub.adapters import Adapters
from hub.clock import Clock
from hub.config import Settings, get_settings
from hub.db import get_session_factory
from hub.engine.definitions import Catalog
from hub.engine.runner import Runner
from hub.engine.scheduler import Scheduler


@dataclass
class Runtime:
    """Everything a process needs, built once: settings, adapters, catalog, sessions."""

    settings: Settings
    adapters: Adapters
    catalog: Catalog
    session_factory: sessionmaker[Session]
    clock: Clock

    @classmethod
    def build(cls, settings: Settings | None = None, clock: Clock | None = None) -> Runtime:
        settings = settings or get_settings()
        adapters = Adapters(settings)
        catalog = Catalog.load(settings.workflows_dir, adapters.registry.names())
        return cls(
            settings=settings,
            adapters=adapters,
            catalog=catalog,
            session_factory=get_session_factory(settings.database_url),
            clock=clock or Clock(),
        )

    def runner(self) -> Runner:
        return Runner(self.session_factory, self.catalog, self.adapters, self.settings, self.clock)

    def scheduler(self) -> Scheduler:
        return Scheduler(self.session_factory, self.catalog, self.adapters, self.clock)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
