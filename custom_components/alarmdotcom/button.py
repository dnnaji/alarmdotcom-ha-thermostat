"""Alarmdotcom implementation of an HA button."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic

import pyalarmdotcomajax as pyadc
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import DiscoveryInfoType

from .const import DATA_HUB, DOMAIN
from .entity import (
    AdcControllerT,
    AdcEntity,
    AdcEntityDescription,
    AdcManagedDeviceT,
)
from .util import cleanup_orphaned_entities_and_devices

if TYPE_CHECKING:
    from .hub import AlarmHub

log = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the binary sensor platform."""

    hub: AlarmHub = hass.data[DOMAIN][config_entry.entry_id][DATA_HUB]

    entities: list[AdcButtonEntity] = []
    for entity_description in ENTITY_DESCRIPTIONS:
        entities.extend(
            AdcButtonEntity(
                hub=hub, resource_id=device.id, description=entity_description
            )
            for device in hub.api.managed_devices.values()
            if entity_description.supported_fn(hub, device.id)
        )

    async_add_entities(entities)

    current_entity_ids = {entity.entity_id for entity in entities}
    current_unique_ids = {
        uid for uid in (entity.unique_id for entity in entities) if uid is not None
    }
    await cleanup_orphaned_entities_and_devices(
        hass, config_entry, current_entity_ids, current_unique_ids, "button"
    )


@dataclass(frozen=True, kw_only=True)
class AdcButtonDescription(
    Generic[AdcManagedDeviceT, AdcControllerT],
    AdcEntityDescription[AdcManagedDeviceT, AdcControllerT],
    ButtonEntityDescription,
):
    """Describes a button entity."""

    press_fn: Callable[[HomeAssistant, AdcManagedDeviceT], Any]


ENTITY_DESCRIPTIONS: list[AdcEntityDescription] = []


class AdcButtonEntity(AdcEntity[AdcManagedDeviceT, AdcControllerT], ButtonEntity):
    """Base Alarm.com binary sensor entity."""

    entity_description: AdcButtonDescription

    async def async_press(self) -> None:
        """Press the button."""
        self.entity_description.press_fn(self.hub.hass, self.resource_id)

    @callback
    def update_state(self, message: pyadc.EventBrokerMessage | None = None) -> None:
        """Update entity state."""
        return
