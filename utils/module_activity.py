from __future__ import annotations

from typing import Self
from weakref import WeakSet

import discord


class ActivityView(discord.ui.View):
    """Count handled component interactions after Discord's item and view checks."""

    def __init__(self, *, timeout: float | None = 180):
        self._instrumented: WeakSet[discord.ui.Item] = WeakSet()
        super().__init__(timeout=timeout)
        for item in self.children:
            self._track(item)

    def add_item(self, item: discord.ui.Item) -> Self:
        super().add_item(item)
        self._track(item)
        return self

    def _track(self, item: discord.ui.Item) -> None:
        if item in self._instrumented or not item.is_dispatchable():
            return
        self._instrumented.add(item)
        callback = item.callback
        module = type(self).__module__.removeprefix("cogs.")

        async def counted(interaction: discord.Interaction) -> None:
            await callback(interaction)
            if interaction.guild_id is not None and not interaction.user.bot:
                interaction.client.dispatch("module_activity", interaction.guild_id, interaction.user.id, module, "component")

        item.callback = counted
