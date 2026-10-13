from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.db.models import EntityLink
from hub.engine.errors import PermanentError


class CrosswalkAdapter(Adapter):
    """Identity map across applications. Lives in the hub database, so it is transactional."""

    system = "crosswalk"
    rate_per_second = 1000.0
    burst = 1000

    def register(self, registry: Registry) -> None:
        self.action(
            registry, "lookup", self.lookup, description="Find an entity in another application"
        )
        self.action(
            registry, "link", self.link, description="Record that several ids are one entity"
        )

    def lookup(
        self, ctx: ActionContext, entity: str, system: str, external_id: str, target: str
    ) -> dict[str, Any]:
        link = ctx.session.execute(
            select(EntityLink).where(
                EntityLink.entity_type == entity,
                EntityLink.system == system,
                EntityLink.external_id == str(external_id),
            )
        ).scalar_one_or_none()
        if link is None:
            return {"found": False, "canonical_id": None, "target_id": None}
        other = ctx.session.execute(
            select(EntityLink).where(
                EntityLink.entity_type == entity,
                EntityLink.canonical_id == link.canonical_id,
                EntityLink.system == target,
            )
        ).scalar_one_or_none()
        return {
            "found": other is not None,
            "canonical_id": str(link.canonical_id),
            "target_id": other.external_id if other else None,
        }

    def link(self, ctx: ActionContext, entity: str, ids: dict[str, Any]) -> dict[str, Any]:
        if not ids:
            raise PermanentError("crosswalk.link needs at least one id")
        clean = {s: str(i) for s, i in ids.items() if i not in (None, "")}
        rows = (
            ctx.session.execute(
                select(EntityLink).where(
                    EntityLink.entity_type == entity,
                    EntityLink.system.in_(list(clean)),
                )
            )
            .scalars()
            .all()
        )
        existing = [e for e in rows if clean.get(e.system) == e.external_id]
        canonicals = {e.canonical_id for e in existing}
        if canonicals:
            canonical = sorted(canonicals, key=str)[0]
            if len(canonicals) > 1:
                for e in ctx.session.execute(
                    select(EntityLink).where(EntityLink.canonical_id.in_(list(canonicals)))
                ).scalars():
                    e.canonical_id = canonical
        else:
            canonical = uuid.uuid4()
        have = {e.system for e in existing}
        created = 0
        for system, external_id in clean.items():
            if system in have:
                continue
            ctx.session.add(
                EntityLink(
                    entity_type=entity,
                    system=system,
                    external_id=external_id,
                    canonical_id=canonical,
                )
            )
            created += 1
        ctx.session.flush()
        return {"canonical_id": str(canonical), "created": created, "merged": len(canonicals) > 1}
