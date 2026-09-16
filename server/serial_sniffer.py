"""Снифър за серийни портове на устройства с непознат протокол.

Слуша порта и записва всеки "кадър" (байтове, разделени от пауза —
FRAME_GAP_S тишина затваря кадъра; при непознат протокол това е
единственият начин да се отгатне рамкирането) като hex + ASCII, с
часовник и разстояние от предишния запис — паузите издават polling
интервалите на устройството. Следят се и контролните линии (CTS/DSR/
CD/RI се четат, RTS/DTR се огледалват в MITM режим) — при устройства
като paddle-и и RTS-keyed PTT интерфейси протоколът Е по линиите, не по
данните.

Изходът отива едновременно в логер "serial_sniffer" на DEBUG ниво (т.е.
в дебъг конзолата на сървъра) и в отделен файл до config.json.

Два режима:
  python -m server.serial_sniffer COM7 9600              # само слуша
  python -m server.serial_sniffer COM7 9600 --mitm COM9  # между устройство и програма

MITM режимът препраща байтовете и линиите в двете посоки и записва всяка
посока отделно — така се виждат и заявките на чуждата програма, не само
отговорите на устройството. Изисква com0com двойка: програмата отваря
единия край, снифърът — другия.

Портът трябва да е различен от портовете на радиата — те са отворени от
техните bridge-ове и второ отваряне или ще гръмне, или ще открадне CAT
връзката. Admin панелът отказва такъв избор (виж _ports_in_use в
server/admin_api.py); при ръчно пускане от команден ред внимавай сам.
"""

import argparse
import asyncio
import contextlib
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import serial
import serial_asyncio

from common.app_paths import app_dir

log = logging.getLogger("serial_sniffer")

FRAME_GAP_S = 0.02    # толкова тишина затваря кадъра
LINE_POLL_S = 0.005   # колко често се четат контролните линии
KEEP_LINES = 500      # последните N реда, които admin панелът показва на живо

DEVICE = "устр."
APP = "прогр."


def _dump(data: bytes) -> str:
    hex_part = " ".join(f"{b:02x}" for b in data)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hex_part}  |{ascii_part}|"


class _Sink:
    """Един ред на събитие — едновременно в дебъг конзолата, във файла и
    в пръстен за admin панела. flush() на всеки ред: снифърът обикновено
    се убива с Ctrl+C и буфериран край на файла би изгубил точно
    последното интересно нещо."""

    def __init__(self, path: Path):
        self.path = path
        self.recent = deque(maxlen=KEEP_LINES)
        self._fh = path.open("a", encoding="utf-8")
        self._last = None

    def _emit(self, text: str):
        now = time.monotonic()
        gap = 0.0 if self._last is None else now - self._last
        self._last = now
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" +{gap:7.3f}s {text}"
        log.debug("%s", line)
        self.recent.append(line)
        self._fh.write(line + "\n")
        self._fh.flush()

    def data(self, direction: str, data: bytes):
        self._emit(f"{direction:>6} {len(data):3d}B  {_dump(data)}")

    def lines(self, direction: str, state: tuple):
        cts, dsr, cd, ri = (int(bool(v)) for v in state)
        self._emit(f"{direction:>6} лин.  CTS={cts} DSR={dsr} CD={cd} RI={ri}")

    def close(self):
        if not self._fh.closed:
            self._fh.close()


class _GapFramer(asyncio.Protocol):
    def __init__(self, direction: str, sink: _Sink):
        self.direction = direction
        self.transport = None
        self.peer = None  # другият _GapFramer в MITM режим
        self._sink = sink
        self._buf = bytearray()
        self._timer = None

    def connection_made(self, transport):
        self.transport = transport
        # Същата причина като в cat_bridge.open_serial: pyserial вдига
        # RTS+DTR при отваряне на порта, което при RTS/DTR-keyed
        # интерфейс включва предавателя само от закачането на снифъра.
        # В MITM режим огледалото веднага ги връща там, където чуждата
        # програма ги държи.
        transport.serial.rts = False
        transport.serial.dtr = False

    def data_received(self, data):
        # Препращането е първо и без буфериране — рамкирането е само за
        # записа, не бива да добавя закъснение към самата връзка.
        if self.peer is not None and self.peer.transport is not None:
            self.peer.transport.write(data)
        self._buf += data
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(FRAME_GAP_S, self._flush)

    def _flush(self):
        if self._buf:
            self._sink.data(self.direction, bytes(self._buf))
            self._buf.clear()

    def connection_lost(self, exc):
        self._flush()
        self.transport = None
        log.warning("серийната връзка (%s) се затвори: %s", self.direction, exc)


async def _watch_lines(ser, direction: str, sink: _Sink, mirror=None):
    """Чете входните линии на един порт и логва САМО при промяна (иначе
    200 еднакви реда в секунда). Първото четене се логва като базово
    състояние — без него не се знае спрямо какво е промяната.

    `mirror`: серийният обект на отсрещния порт в MITM режим. Огледалото
    е задължително, не украса — com0com пренася RTS→CTS и DTR→DSR, така
    че без него RTS-keyed PTT на чуждата програма никога не стига до
    устройството и снифърът чупи връзката, която уж само наблюдава."""
    last = None
    while True:
        try:
            state = (ser.cts, ser.dsr, ser.cd, ser.ri)
        except (OSError, serial.SerialException, AttributeError):
            return  # портът се затвори под нас — не е грешка, просто край
        if state != last:
            sink.lines(direction, state)
            if mirror is not None:
                try:
                    mirror.rts, mirror.dtr = bool(state[0]), bool(state[1])
                except (OSError, serial.SerialException):
                    log.warning("огледалото на линиите към отсрещния порт се провали", exc_info=True)
            last = state
        await asyncio.sleep(LINE_POLL_S)


def default_log_path(port: str) -> Path:
    # "/dev/ttyUSB0" съдържа наклонени черти, а "COM7.log" е резервирано
    # име на Windows — затова името се чисти, а не се ползва направо.
    safe = "".join(c if c.isalnum() else "_" for c in port)
    return app_dir(__file__) / f"sniff-{safe}-{datetime.now():%Y%m%d-%H%M%S}.log"


class SnifferSession:
    """Една активна сесия. Ползва се и от командния ред, и от admin
    панела — затова отварянето/затварянето живее тук, а не в main()."""

    def __init__(self, port: str, baud: int, mitm: str | None = None, out=None):
        self.port = port
        self.baud = baud
        self.mitm = mitm
        self.path = Path(out) if out else default_log_path(port)
        self.started_at = None
        self.sink = None
        self._transports = []
        self._tasks = []

    @property
    def running(self) -> bool:
        return bool(self._transports)

    async def start(self):
        self.sink = _Sink(self.path)
        loop = asyncio.get_running_loop()
        try:
            dev_t, dev = await serial_asyncio.create_serial_connection(
                loop, lambda: _GapFramer(DEVICE, self.sink), self.port, baudrate=self.baud
            )
            self._transports.append(dev_t)
            app_t = None
            if self.mitm:
                app_t, app = await serial_asyncio.create_serial_connection(
                    loop, lambda: _GapFramer(APP, self.sink), self.mitm, baudrate=self.baud
                )
                self._transports.append(app_t)
                dev.peer, app.peer = app, dev
            self._tasks.append(asyncio.create_task(
                _watch_lines(dev_t.serial, DEVICE, self.sink, mirror=app_t.serial if app_t else None)
            ))
            if app_t:
                self._tasks.append(asyncio.create_task(_watch_lines(app_t.serial, APP, self.sink, mirror=dev_t.serial)))
        except Exception:
            # Без това един провалил се MITM порт оставя вече отворения
            # пръв порт зает — и следващият опит гърми с "access denied"
            # на порт, който никой не ползва.
            await self.stop()
            raise
        # Логерът се вдига на DEBUG насила: сървърът върви на INFO по
        # подразбиране, иначе снифърът щеше да пише във файла, но нищо да
        # не се вижда в конзолата, без ръчна разходка до Debug диалога.
        log.setLevel(logging.DEBUG)
        self.started_at = time.time()
        log.info("снифър: %s @ %s baud%s -> %s", self.port, self.baud,
                 f", MITM към {self.mitm}" if self.mitm else "", self.path)

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        for transport in self._transports:
            transport.close()
        self._transports = []
        if self.sink:
            self.sink.close()  # пръстенът остава — панелът още показва последните редове
            # Празен файл = нищо не е записано, най-често защото портът
            # изобщо не се отвори. Няма смисъл да трупа боклук до
            # config.json при всеки сбъркан опит от admin панела.
            with contextlib.suppress(OSError):
                if self.sink.path.stat().st_size == 0:
                    self.sink.path.unlink()
        log.setLevel(logging.NOTSET)
        self.started_at = None

    def state(self) -> dict:
        return {
            "running": self.running,
            "port": self.port,
            "baud": self.baud,
            "mitm": self.mitm,
            "path": str(self.path),
            "started_at": self.started_at,
        }

    def tail(self, n: int) -> list:
        return list(self.sink.recent)[-n:] if self.sink else []


async def sniff(port: str, baud: int, mitm: str | None = None, out=None):
    session = SnifferSession(port, baud, mitm, out)
    await session.start()
    try:
        await asyncio.Event().wait()  # до Ctrl+C
    finally:
        await session.stop()


class _FakeSerial:
    """Достатъчно от pyserial.Serial за самопроверката на _watch_lines."""

    def __init__(self):
        self.cts = self.dsr = self.cd = self.ri = False
        self.rts = self.dtr = None


async def _self_check():
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    sink = _Sink(tmp / "sniff.log")

    framer = _GapFramer(DEVICE, sink)
    framer.data_received(b"\xfe\xfe")
    framer.data_received(b"\x00\xe0\x03\xfd")  # същия изблик — един кадър
    await asyncio.sleep(FRAME_GAP_S * 3)
    framer.data_received(b"\xfe\xfe\xe0\x00")  # след паузата — втори кадър
    await asyncio.sleep(FRAME_GAP_S * 3)

    ser, mirror = _FakeSerial(), _FakeSerial()
    watcher = asyncio.create_task(_watch_lines(ser, APP, sink, mirror=mirror))
    await asyncio.sleep(LINE_POLL_S * 6)  # базовото състояние — един ред
    ser.cts = True                        # чуждата програма вдига RTS (PTT)
    await asyncio.sleep(LINE_POLL_S * 6)  # промяната — още един ред
    await asyncio.sleep(LINE_POLL_S * 6)  # без промяна — НЕ трябва да пише повече
    watcher.cancel()
    sink.close()

    lines = (tmp / "sniff.log").read_text(encoding="utf-8").splitlines()
    data_lines = [ln for ln in lines if "лин." not in ln]
    line_lines = [ln for ln in lines if "лин." in ln]

    assert len(data_lines) == 2, f"рамкиране по пауза: очаквах 2 кадъра, получих {len(data_lines)}: {data_lines}"
    assert "fe fe 00 e0 03 fd" in data_lines[0], data_lines[0]
    assert "fe fe e0 00" in data_lines[1], data_lines[1]
    assert "|......|" in data_lines[0], "липсва ASCII колоната"

    assert len(line_lines) == 2, f"линиите трябва да се логват само при промяна, получих {len(line_lines)}: {line_lines}"
    assert "CTS=0 DSR=0 CD=0 RI=0" in line_lines[0], line_lines[0]
    assert "CTS=1" in line_lines[1], line_lines[1]
    assert mirror.rts is True, "RTS не беше огледален към отсрещния порт — MITM чупи line-keyed PTT"
    assert mirror.dtr is False

    assert len(sink.recent) == 4, "пръстенът за admin панела трябва да пази същите редове"
    assert default_log_path("/dev/ttyUSB0").name.startswith("sniff-_dev_ttyUSB0-"), "името на файла не е изчистено"
    print("serial_sniffer.py: ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Записва трафика на сериен порт с непознат протокол")
    parser.add_argument("port", nargs="?", help="напр. COM7 (НЕ порт на радио)")
    parser.add_argument("baud", nargs="?", type=int, default=9600)
    parser.add_argument("--mitm", help="втори порт (com0com двойка), към който е закачена чуждата програма")
    parser.add_argument("--out", type=Path, help="път до файла (по подразбиране sniff-<порт>-<време>.log)")
    parser.add_argument("--self-check", action="store_true", help="проверка без хардуер")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.self_check:
        asyncio.run(_self_check())
    elif not args.port:
        parser.error("трябва порт (или --self-check)")
    else:
        try:
            asyncio.run(sniff(args.port, args.baud, args.mitm, args.out))
        except KeyboardInterrupt:
            pass
