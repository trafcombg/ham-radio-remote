"""One TP-Link Tapo smart plug's server-side bridge — a thin wrapper over
python-kasa's Device (handles the KLAP/legacy encrypted handshake so we
don't have to). Polls on/off state, exposes manual turn_on()/turn_off()
for the admin panel, and — when linked to a radio or amplifier — keeps
the plug's power matched to whether that device is currently running.

The link is re-checked on every poll tick (not wired once at connect
time): same "reach into the other manager's .bridges dict" pattern
amplifier_bridge.py's linked_radio already uses, just live instead of a
one-shot wiring, so it self-heals regardless of start order (plug config
loaded before or after the radio it powers) or which one gets
reloaded/reactivated later.
"""

import asyncio
import logging

from kasa import Credentials, Device, Discover, KasaException

log = logging.getLogger("tapo_bridge")

POLL_INTERVAL_S = 5.0  # a mains relay has no need for sub-second polling


class TapoBridge:
    def __init__(self, cfg: dict, db, radio_manager=None, amp_manager=None):
        self.cfg = cfg
        self.name = cfg["name"]
        self.db = db
        self.radio_manager = radio_manager
        self.amp_manager = amp_manager
        self.device: Device | None = None
        self.is_on: bool | None = None  # None until the first successful poll
        self.online = False
        self._poll_task = None

    async def start(self):
        # Device.connect(config=...) needs the device's protocol/encryption
        # family already known (KLAP vs. the legacy plain "IOT" protocol) —
        # without it, it guessed wrong for this P100 (SMART.TAPOPLUG/KLAP)
        # and tried the old XOR protocol on port 9999, which the device
        # doesn't even listen on. discover_single() runs the same discovery
        # probe the `kasa` CLI uses to detect this correctly, then connects.
        credentials = Credentials(self.cfg.get("username") or "", self.cfg.get("password") or "")
        self.device = await Discover.discover_single(self.cfg["host"], credentials=credentials)
        await self._refresh()
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("Tapo plug %s up (%s)", self.name, self.cfg["host"])

    async def shutdown(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self.device:
            await self.device.disconnect()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            await self._refresh()
            await self._sync_link()

    async def _refresh(self):
        try:
            await self.device.update()
            self.is_on = self.device.is_on
            self.online = True
        except KasaException:
            self.online = False
            log.warning("Tapo plug %s unreachable", self.name, exc_info=True)

    async def _sync_link(self):
        if not self.online:
            return  # don't try to key a plug we can't currently reach
        linked_radio = self.cfg.get("linked_radio")
        linked_amp = self.cfg.get("linked_amplifier")
        if linked_radio and self.radio_manager:
            should_be_on = linked_radio in self.radio_manager.bridges
        elif linked_amp and self.amp_manager:
            should_be_on = linked_amp in self.amp_manager.bridges
        else:
            return  # no link configured — manual control only
        if should_be_on and not self.is_on:
            await self.turn_on()
        elif not should_be_on and self.is_on:
            await self.turn_off()

    async def turn_on(self):
        await self.device.turn_on()
        self.is_on = True

    async def turn_off(self):
        await self.device.turn_off()
        self.is_on = False


if __name__ == "__main__":
    import asyncio

    from server.db import NullDb

    class _FakeDevice:
        def __init__(self):
            self.is_on = False
            self.turn_on_calls = 0
            self.turn_off_calls = 0

        async def update(self):
            pass

        async def turn_on(self):
            self.is_on = True
            self.turn_on_calls += 1

        async def turn_off(self):
            self.is_on = False
            self.turn_off_calls += 1

        async def disconnect(self):
            pass

    class _FakeManager:
        def __init__(self):
            self.bridges = {}

    async def _demo_manual_on_off():
        bridge = TapoBridge({"name": "SHACK-PLUG", "host": "127.0.0.1"}, NullDb())
        bridge.device = _FakeDevice()
        bridge.is_on = False
        bridge.online = True

        await bridge.turn_on()
        assert bridge.device.is_on is True
        assert bridge.is_on is True

        await bridge.turn_off()
        assert bridge.device.is_on is False
        assert bridge.is_on is False

    async def _demo_linked_radio_sync():
        # Reproduces the requested behavior: the plug should track a
        # linked radio's running state, checked live on every poll tick
        # rather than wired once — so it must react whichever order the
        # radio starts/stops relative to the plug's own config load.
        radio_manager = _FakeManager()
        bridge = TapoBridge(
            {"name": "RADIO-PLUG", "host": "127.0.0.1", "linked_radio": "IC-7300"},
            NullDb(), radio_manager=radio_manager,
        )
        bridge.device = _FakeDevice()
        bridge.is_on = False
        bridge.online = True

        await bridge._sync_link()
        assert bridge.is_on is False, "radio not running yet — plug must stay off"

        radio_manager.bridges["IC-7300"] = object()  # radio now running
        await bridge._sync_link()
        assert bridge.is_on is True, "linked radio started — plug must turn on"

        del radio_manager.bridges["IC-7300"]
        await bridge._sync_link()
        assert bridge.is_on is False, "linked radio stopped — plug must turn off"

    async def _demo_offline_skips_sync():
        radio_manager = _FakeManager()
        radio_manager.bridges["IC-7300"] = object()
        bridge = TapoBridge(
            {"name": "RADIO-PLUG", "host": "127.0.0.1", "linked_radio": "IC-7300"},
            NullDb(), radio_manager=radio_manager,
        )
        bridge.device = _FakeDevice()
        bridge.is_on = False
        bridge.online = False  # last refresh failed — device unreachable

        await bridge._sync_link()
        assert bridge.device.turn_on_calls == 0, "must not try to key a plug we can't currently reach"

    asyncio.run(_demo_manual_on_off())
    asyncio.run(_demo_linked_radio_sync())
    asyncio.run(_demo_offline_skips_sync())
    print("tapo_bridge.py: ok")
