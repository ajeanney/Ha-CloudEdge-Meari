"""Swap cloudplus's Meari cloud client and P2P streamer for local fakes.

Only loaded by the e2e harness (``cloudplus_e2e_sim:`` in configuration.yaml).
The harness creates the cloudplus entry after HA has started, so the patches
are in place before cloudplus builds any client or streamer.
"""

from __future__ import annotations

import importlib
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.requirements import async_get_integration_with_requirements

from .fake_camera import FakeApiClient, FakeP2PStreamer, build_fake_api_client

DOMAIN = "cloudplus_e2e_sim"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Patch every place cloudplus binds its cloud client and streamer."""
    del config
    integration = await async_get_integration_with_requirements(hass, "cloudplus")
    package = await integration.async_get_component()
    config_flow = await integration.async_get_platform("config_flow")
    coordinator = await hass.async_add_executor_job(
        importlib.import_module, "custom_components.cloudplus.coordinator"
    )
    package.build_api_client = build_fake_api_client
    config_flow.build_api_client = build_fake_api_client
    coordinator.MeariApiClient = FakeApiClient
    coordinator.P2PStreamer = FakeP2PStreamer
    _LOGGER.warning("cloudplus is running against the simulated camera")
    return True
