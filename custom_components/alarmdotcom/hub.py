"""Controller interfaces with the Alarm.com API via pyalarmdotcomajax."""

import asyncio
import logging
from functools import partial
from datetime import timedelta

import pyalarmdotcomajax as pyadc
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from pyalarmdotcomajax import AlarmBridge
from pyalarmdotcomajax.events import EventBrokerTopic

from .const import (
    CONF_MFA_TOKEN,
    DATA_HUB,
    DOMAIN,
    PLATFORMS,
)

log = logging.getLogger(__name__)


class AlarmHub:
    """Config-entry initiated Alarm Hub."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the system."""

        self.hass: HomeAssistant = hass
        self.config_entry: ConfigEntry = config_entry

        self.api = AlarmBridge(
            username=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
            mfa_token=config_entry.data.get(CONF_MFA_TOKEN),
        )

        self.close_jobs: list[CALLBACK_TYPE] = []

        # Track recent websocket activity to detect staleness / desync.
        self._last_event_ts: float | None = None
        self._refresh_lock = asyncio.Lock()

        hass.data.setdefault(DOMAIN, {})[self.config_entry.entry_id] = {DATA_HUB: self}

        self.available: bool = True

    # @property
    # def available(self) -> bool:
    #     """
    #     Whether the Alarm.com API is available.

    #     This will only be true if the websocket connection is established and has not been disconnected
    #     for more than 60 seconds. This is to prevent the system from being marked as unavailable if the
    #     connection is temporarily lost.
    #     """
    #     # If never connected, treat as unavailable.
    #     ws = self.api.ws_controller
    #     if ws.connected:
    #         # Update last connected time
    #         self._last_connected = asyncio.get_event_loop().time()
    #         return True

    #     # If never set, treat as unavailable
    #     last_connected = getattr(self, "_last_connected", None)
    #     if last_connected is None:
    #         return False

    #     # If disconnected for less than 60 seconds, still available
    #     return bool(asyncio.get_event_loop().time() - last_connected < 60)

    async def login(self) -> bool:
        """Log in to alarm.com."""

        try:
            await self.api.login()
        except pyadc.AuthenticationFailed as err:
            raise ConfigEntryAuthFailed from err
        except pyadc.MustConfigureMfa:
            log.error(
                "Alarm.com requires that two-factor authentication be set up on your account. "
                "Please log in to Alarm.com and set up two-factor authentication."
            )
            return False
        except Exception as err:
            log.error("Unexpected error during Alarm.com login: %s", err)
            return False

        return True

    async def initialize(self) -> bool:
        """Initialize connection to Alarm.com after user-driven authentication has already taken place."""

        setup_ok = False

        try:
            async with asyncio.timeout(10):
                await self.api.initialize()
            setup_ok = True
        except (
            TimeoutError,
            pyadc.UnexpectedResponse,
            pyadc.ServiceUnavailable,
        ) as err:
            raise ConfigEntryNotReady("Could not connect to Alarm.com.") from err
        except pyadc.AuthenticationException as err:
            raise ConfigEntryAuthFailed from err
        except Exception:
            log.exception("Unexpected error during Alarm.com initialization.")
            return False
        finally:
            if not setup_ok:
                await self.api.close()

        # Initialize WebSocket connection.
        await self.api.start_event_monitoring(partial(_ws_state_handler, self))

        # Track resource updates to know websocket is alive and delivering events.
        self.close_jobs.append(
            self.api.subscribe(
                self._mark_event_activity,
                ids=None,
            )
        )

        # Fallback: periodic full refresh if websocket has been quiet or down.
        self.close_jobs.append(
            async_track_time_interval(
                self.hass,
                self._refresh_if_stale,
                timedelta(minutes=15),
            )
        )

        # Ensure the Alarm.com websocket is closed cleanly when HA shuts down. Without this,
        # the remote end can keep the connection alive for a while after HA stops, which can
        # contribute to "too many connections" errors on restart and leave the integration
        # temporarily de-synced.
        self.close_jobs.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP,
                lambda event: self.hass.async_create_task(self.api.close()),
            )
        )

        self.close_jobs.append(self.config_entry.add_update_listener(_update_listener))

        # Create system/hub device.
        device_registry = dr.async_get(self.hass)

        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, self.api.active_system.id)},
            manufacturer="Alarm.com",
            name=self.api.active_system.name,
            entry_type=dr.DeviceEntryType.SERVICE,
        )

        return True

    @callback
    def _mark_event_activity(self, message: pyadc.EventBrokerMessage) -> None:
        """Record that we received an event via websocket."""

        if message.topic in [
            pyadc.EventBrokerTopic.RESOURCE_ADDED,
            pyadc.EventBrokerTopic.RESOURCE_UPDATED,
            pyadc.EventBrokerTopic.CONNECTION_EVENT,
        ]:
            self._last_event_ts = _now()

    async def _refresh_if_stale(self, *_: object) -> None:
        """Periodic safeguard: refresh state if websocket looks quiet or down."""

        # Don't run overlapping refreshes.
        if self._refresh_lock.locked():
            return

        async with self._refresh_lock:
            now = _now()
            # If we've never seen an event, or it's been > 20 minutes, refresh.
            if self._last_event_ts is None or (now - self._last_event_ts) > (20 * 60):
                log.info("Alarm.com websocket quiet for >20 minutes; running safety refresh.")
                try:
                    await self.api.fetch_full_state()
                    self._last_event_ts = now
                except Exception:  # noqa: BLE001 - log and continue
                    log.exception("Alarm.com safety refresh failed.")

    async def close(self) -> bool:
        """
        Reset this bridge to default state.

        Will cancel any scheduled setup retry and will unload
        the config entry.
        """

        while self.close_jobs:
            self.close_jobs.pop()()

        await self.api.close()

        unload_success: bool = await self.hass.config_entries.async_unload_platforms(
            self.config_entry, PLATFORMS
        )

        return unload_success
async def _ws_state_handler(hub: "AlarmHub", message: pyadc.EventBrokerMessage) -> None:
    """Handle changes to websocket state for ConfigEntry and logging."""

    if not isinstance(message, pyadc.ConnectionEvent):
        return

    # WebSocket service handles reconnection on its own. Handle reporting for DEAD state here; do not attempt to
    # reconnect independently.

    if message.current_state == pyadc.WebSocketState.DEAD:
        log.error("Alarm.com websocket state message: %s", message)
        raise ConfigEntryNotReady("Alarm.com websocket connection died.")

    if message.current_state not in [
        pyadc.WebSocketState.CONNECTED,
        pyadc.WebSocketState.CONNECTING,
    ]:
        log.info("Alarm.com websocket state message: %s", message)
        return

    # Should only print CONNECTED events.
    log.debug("Alarm.com websocket state: %s", message.current_state)

    # On (re)connect, mark the websocket as active to prevent unnecessary refreshes.
    hub._last_event_ts = asyncio.get_event_loop().time()


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle ConfigEntry options update."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _now() -> float:
    """Return monotonic time for activity tracking."""

    return asyncio.get_event_loop().time()
