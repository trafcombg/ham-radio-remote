# HAM Radio Remote — Фаза 1 + Фаза 2 + Фаза 3 + Фаза 4

Клиент-сървър дистанционно управление на Icom радиостанции: CAT bridge
(виртуален COM порт → мрежа → реален CAT сериен порт) + двупосочно аудио
(UDP) + PTT арбитраж между потребители + уеб admin панел. Виж
[ham-radio-remote-plan.md](ham-radio-remote-plan.md) за пълния план по фази.

**Аудио бележка:** PyOgg-ната bundled `opus.dll` се оказа счупена на
decode (потвърдено с два независими ctypes wrapper-а — encode работи,
decode винаги връща тишина). Засега аудиото е uncompressed PCM вместо
Opus — за LAN честотната лента не е проблем (~768kbps mono/48kHz), а и
latency-то е по-ниско без encode/decode. Виж `common/audio_io.py`.

## Какво добавя Фаза 4 към Фаза 3

CW модул + RC-28. **Важно за тестване тук:** нямам paddle, WinKeyer или
RC-28 хардуер в тази среда, така че тези конкретни пътища не са
демонстрирани end-to-end — само компилирани/логически проверени.

- Унифициран CW event stream: `common/cw_link.py` — отделен UDP канал
  per радио (не аудио/CAT), event-driven на сървъра (`loop.add_reader`,
  не polling) за минимално закъснение
- CW keying на сървъра използва **същия** `PttArbiter` lock като гласово
  PTT: първо key-down от свободно радио го "заема" автоматично за
  потребителя, друг потребител бива тихо игнориран, а lock-ът пада сам
  1.5s след последния key-up (paddle никога не казва изрично "приключих")
  — виж self-check-а в `server/radio_bridge.py`
- CW изходен метод (`cw.method`: civ/rts/dtr) е конфигурируем per радио
  от admin панела, по подразбиране same-as-PTT (реалистично за повечето
  радиа — ключуването за CW обикновено е същата физическа линия)
- Клиентски CW източници (`client/cw/`):
  - `iambic.py` — Mode A iambic keyer, чиста state machine, **тествана
    без хардуер** (self-check)
  - `text_source.py` — текст→Morse, върху `common/morse.py` (тестван)
  - `straight_key.py` — прав ключ, вкаран в клиентския UI (бутон)
  - `serial_paddle.py`, `winkeyer.py` — реален paddle през USB-сериен
    (CTS/DSR) и WinKeyer passthrough — код написан, **не тествано на
    реален хардуер тук**, пин мапинг/статус байтове могат да се нуждаят
    от корекция (виж ponytail бележките в самите файлове)
- RC-28: `client/rc28.py` — dial delta → CI-V смяна на честота. Протоколът
  на RC-28 НЕ Е документиран (плана изрично го казва) и изисква USB
  capture (Wireshark+USBPcap) за потвърждение, който нямам тук.
  `parse_report()` е маркиран като **непотвърден placeholder**; всичко
  надолу по веригата (delta → нова честота → CI-V set-frequency команда)
  е тествано и коректно — виж self-check-а и `common/civ.py`.

## Какво добавя Фаза 3 към Фаза 2

- FastAPI admin панел (`server/admin_api.py`), достъпен отдалечено от
  всеки компютър в мрежата (bind на `0.0.0.0`, не само localhost)
- Радио конфигурацията вече се пази в PostgreSQL (`radio_configs`
  таблица), с hot-reload на отделно радио без рестарт на сървъра
  (`server/radio_manager.py`)
- Test/Verify бутон в панела — активна CAT проверка (CI-V "get
  frequency") преди да приложиш конфигурация
- Ролеви достъп: admin login с потребител+парола (`server/create_admin.py`,
  `server/web_auth.py`) — обикновените потребители продължават да имат
  само десктоп клиента, без парола (непроменено от Фаза 1/2)
- Ако радио се преконфигурира докато е заето: заявката се отказва с
  409 + кой го държи, освен ако admin-ът не потвърди (`force`) — засегнатият
  клиент получава ясно съобщение и връзката пада контролирано

## Структура (нови/променени спрямо Фаза 2)

```
server/admin_api.py    FastAPI: login, /api/radios CRUD, /api/devices, Test/Verify
server/web/            admin.html + login.html (vanilla HTML/JS, без build стъпка)
server/web_auth.py     подписани session cookies (itsdangerous)
server/radio_manager.py  start/stop/reload на радиа by runtime, seed от config.json в DB
server/create_admin.py CLI: python -m server.create_admin <user> <парола>
server/db.py             +auth (pbkdf2 hash), +radio_configs CRUD
server/cat_bridge.py    +probe_serial_port() за Test/Verify, make_cat_server() разделен от serve_forever
server/radio_bridge.py +shutdown()/test_cat(), за hot-reload и Test/Verify на running радио
```

## Инсталация

```bash
pip install -r server/requirements.txt
pip install -r client/requirements.txt
```

com0com виртуална двойка портове (по една на радио) все още се
инсталира ръчно — packaging е Фаза 7.

## Конфигурация

1. Пусни `python -m server.list_devices`, за да видиш реалните VID/PID/
   сериен номер на CAT адаптерите и `endpoint_id` на аудио устройствата.
2. Първо стартиране: `server/config.json` — списък `"radios"` — служи
   само като seed. Ако `db.dsn` е зададен и таблицата `radio_configs` е
   празна, сървърът я зарежда там еднократно; след това конфигурацията
   се управлява през admin панела, не през config.json.
3. `db.dsn` вече е практически задължителен за Фаза 3 (login и radio
   CRUD изискват PostgreSQL) — приложи схемата: `psql <dsn> -f server/db/schema.sql`.
   Без `dsn` bridge-овете пак тръгват от config.json, но admin панелът
   е read-only/недостъпен за login.
4. Създай admin потребител: `python -m server.create_admin ivan парола123`.
5. `client/config.json` / `client/config.ic746.json` — непроменени
   спрямо Фаза 2.

## Стартиране

```bash
# сървър (CAT/аудио bridge-ове + admin панел на :8080)
python -m server.main

# клиенти — непроменено спрямо Фаза 2
python -m client.main
python -m client.main config.ic746.json
```

Admin панел: `http://<IP на сървъра>:8080/` от кой да е компютър в
мрежата (не само localhost).

## Тест

**Admin панел отдалечено:** от друг компютър в мрежата отвори
`http://<server-ip>:8080/`, влез с admin потребителя, виж списъка с
радиа + статус (свободно/заето от X).

**Смяна на CAT порт/PTT метод "в движение":** редактирай радио в
панела (напр. смени `serial_port` или `ptt.method`), натисни "Провери
връзка" за активна CAT проверка, после "Запази" — радиото се
рестартира само то, останалите продължават да работят без прекъсване.

**Заето радио:** докато потребител държи PTT на радио, опитай да
запазиш промяна в конфигурацията му от панела — трябва да получиш
предупреждение "заето от X" и избор дали да продължиш; ако потвърдиш,
свързаният клиент вижда ясно съобщение, че радиото е преконфигурирано.

## Тест на CW (без хардуер)

Клиентският CW панел (прав ключ бутон + текст→CW поле) работи с
клавиатура/мишка, без специален хардуер — задръж "CW ключ" или прати
текст, провери в server логовете, че `cw_method.set()` (CI-V или RTS/DTR,
според конфигурацията) реално се вика. За paddle/WinKeyer/RC-28 трябва
реалният хардуер на потребителя — виж бележките по-горе.

## Самопроверки (без хардуер/база)

```bash
python -m server.ptt_arbiter
python -m server.device_registry
python -m server.db
python -m server.radio_bridge
python -m common.civ
python -m common.morse
python -m client.cw.iambic
python -m client.rc28
```
