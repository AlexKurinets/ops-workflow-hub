from __future__ import annotations

from hub.adapters.accounting import AccountingAdapter
from hub.adapters.ats import AtsAdapter
from hub.adapters.base import Action, ActionContext, Adapter, Registry
from hub.adapters.crm import CrmAdapter
from hub.adapters.crosswalk import CrosswalkAdapter
from hub.adapters.email import EmailAdapter
from hub.adapters.esign import EsignAdapter
from hub.adapters.faults import FaultInjector
from hub.adapters.hubactions import HubAdapter
from hub.adapters.payroll import PayrollAdapter
from hub.adapters.sandbox import SandboxStore
from hub.adapters.sheets import SheetsAdapter
from hub.adapters.slack import SlackAdapter
from hub.config import Settings

ADAPTER_CLASSES: tuple[type[Adapter], ...] = (
    AtsAdapter,
    CrmAdapter,
    EsignAdapter,
    PayrollAdapter,
    AccountingAdapter,
    SlackAdapter,
    SheetsAdapter,
    EmailAdapter,
    CrosswalkAdapter,
    HubAdapter,
)


class Adapters:
    """Every adapter, the shared sandbox store and fault injector, and the action registry."""

    def __init__(self, settings: Settings, store: SandboxStore | None = None) -> None:
        self.settings = settings
        self.store = store or SandboxStore()
        self.faults = FaultInjector(self.store)
        self.registry = Registry()
        self.by_system: dict[str, Adapter] = {}
        for cls in ADAPTER_CLASSES:
            adapter = cls(self.store, self.faults, settings)
            adapter.register(self.registry)
            self.by_system[adapter.system] = adapter

    def __getitem__(self, system: str) -> Adapter:
        return self.by_system[system]


__all__ = [
    "ADAPTER_CLASSES",
    "Action",
    "ActionContext",
    "Adapter",
    "Adapters",
    "FaultInjector",
    "Registry",
    "SandboxStore",
]
