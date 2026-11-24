"""ccx's difficulty router for Hermes (``delegation.backend: ccx-router``).

ccx writes this backend's settings when it launches Hermes (``ccx --harness hermes ...``):
``delegation.backends.ccx-router`` holds the ccx adapter route (base_url, api_key, api_mode) and the
router levels, each ``{name, model, description, range}``. Every delegate_task task names its
``difficulty``; the child runs on that level's placeholder model (``ccx-live-<level>``), which the
ccx adapter maps to the model picked for that level in this launch.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider, DelegationRouteError

ROUTE_FIELDS = ("base_url", "api_key", "api_mode")


class CcxRouter(DelegationProvider):
    """Route each task to the ccx router level its difficulty names."""

    @property
    def name(self) -> str:
        return "ccx-router"

    @property
    def display_name(self) -> str:
        return "ccx router"

    @property
    def summary(self) -> str:
        return "Each subagent runs on the ccx router level its difficulty names."

    def levels(self, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        levels = self.settings(config).get("levels")
        return [dict(level) for level in levels or () if isinstance(level, Mapping)
                and isinstance(level.get("name"), str) and isinstance(level.get("model"), str)]

    def is_available(self) -> bool:
        return True

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        levels = self.levels(config)
        if not levels:
            return {}
        return {"difficulty": {
            "type": "string", "enum": [level["name"] for level in levels],
            "description": "How hard this task is; it picks the subagent's model. "
                           + " ".join(f"{level['name']}: {level.get('description', '')}" for level in levels)}}

    def description(self, config: Mapping[str, Any]) -> str:
        names = ", ".join(level["name"] for level in self.levels(config))
        return (f"Give every task a difficulty ({names}): easier tasks run on cheaper, faster models. "
                "Judge the task itself, not how long its text is.") if names else ""

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        levels = {level["name"]: level for level in self.levels(config)}
        if not levels:
            raise DelegationRouteError("The ccx router has no levels; relaunch with `ccx --harness hermes ...`.")
        pick = task.get("difficulty")
        if pick not in levels:
            raise DelegationRouteError(f"Set difficulty to one of: {', '.join(levels)}.")
        settings = self.settings(config)
        route = {key: settings[key] for key in ROUTE_FIELDS if isinstance(settings.get(key), str) and settings[key]}
        return {"provider": "custom", **route, "model": levels[pick]["model"], "label": f"ccx {pick}"}


def register(ctx) -> None:
    ctx.register_delegation_provider(CcxRouter())
