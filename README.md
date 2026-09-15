# HAM Radio Remote — Фаза 1 + Фаза 2 + Фаза 3 + Фаза 4 + Фаза 5 + Фаза 6 + Фаза 7

Клиент-сървър дистанционно управление на Icom радиостанции: CAT bridge
(виртуален COM порт → мрежа → реален CAT сериен порт) + двупосочно аудио
(UDP) + PTT арбитраж между потребители + уеб admin панел. Виж
[ham-radio-remote-plan.md](ham-radio-remote-plan.md) за пълния план по фази.

**Аудио бележка:** PyOgg-ната bundled `opus.dll` се оказа счупена на
decode (потвърдено с два независими ctypes wrapper-а — encode работи,
decode винаги връща тишина). Засега аудиото е uncompressed PCM вместо
Opus — за LAN честотната лента не е проблем (~768kbps mono/48kHz), а и
latency-то е по-ниско без encode/decode. Виж `common/audio_io.py`.

## Следфазови подобрения (по обратна връзка от реална инсталация)

- **Избор на сървър в клиента**: "Настройки" екрана вече има поле
  "Сървър" (IP/име) — смяната преконфигурира всички връзки (status
  watchers + активното радио) без рестарт на клиента. Преди беше
  само в `config.json`, без UI.
- **Volume контрол навсякъде**: `AudioLink` (споделен client+server)
  вече има `mic_gain`/`speaker_gain` с clipping (не wraparound
  изкривяване). В клиента — два слайдъра (0-200%) на основния екран,
  веднага действащи + записват се в config.json. В admin панела —
  полета "Вход"/"Изход сила" per радио (пазят се в `radio_configs`).
- **DB схемата се прилага автоматично** при всяко стартиране на
  сървъра (`PostgresDb.connect()` пуска `schema.sql`,
  `CREATE TABLE IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS` навсякъде —
  безопасно да се изпълнява многократно) — вече няма ръчна `psql -f
  schema.sql` стъпка.
- **`/setup` — първи admin от браузъра**: ако DB е конфигурирана но
  няма нито един admin, `/login` пренасочва автоматично към `/setup`,
  където се създава първият admin потребител без нужда от
  `create_admin.py` CLI.
- **Dropdown избор на устройства** в admin панела: CAT сериен порт и
  аудио устройство вече се избират от реално сканирани списъци
  (`/api/devices`), не се въвеждат ръчно VID/PID hex — точно каквото
  Фаза 2/3 планираха, но не беше wired в самия form до сега.

## Какво добавя Фаза 7 към Фаза 6

Пакетиране в самостоятелни `.exe` + инсталатори + auto-update. Пълни
детайли, build инструкции и какво реално е тествано: [packaging/README.md](packaging/README.md).

- `packaging/server.spec` / `packaging/client.spec` — PyInstaller. **Реално
  build-нах и двата тук** — сървърният .exe действително обслужва admin
  панела (curl-нах го), клиентският .exe реално стартира Qt+asyncio.
- `packaging/installer/server.iss` / `client.iss` — Inno Setup. **Реално
  компилирах и двата** (инсталирах Inno Setup 6 за целта) в работещи
  `.exe` инсталатори — не съм ги пускал (admin elevation + системни
  промени). Клиентът: silent com0com install + desktop/start menu
  shortcuts, без auto-start. Сървърът: опционален silent PostgreSQL
  install, деинсталацията пита дали да запази PostgreSQL/логовете.
- `common/updater.py` + `common/version.py` — проверка на GitHub
  releases; клиентът предлага едно-кликово "Обнови" в UI, сървърът
  сваля автоматично но не се саморестартира (рисково посред сесия) —
  операторът пуска сваления инсталатор ръчно.
- `common/app_paths.py` — `config.json` се чете от папката на .exe-то
  когато е frozen, не отвътре в bundle-а.

## Какво добавя Фаза 6 към Фаза 5

Финален клиентски UI — един клиент, списък с радиа, превключване с едно
действие. Проверено headless (`QT_QPA_PLATFORM=offscreen`): прозорецът
се конструира, списъкът с радиа се пълни, PTT/CW бутоните и Settings
диалогът работят без грешки. Самата CAT/аудио връзка изисква реален
com0com порт, който нямам тук.

- **Конфигурацията на клиента се промени** — вместо един клиент = едно
  радио (Фаза 1-5), сега `client/config.json` има `"radios"` списък и
  ЕДИН клиент може да превключва между всички. Локалният виртуален COM
  порт, аудио устройството и CW/RC-28 target-ът се пренасочват
  автоматично при превключване — CAT софтуер като WSJT-X си стои сочещ
  към същия локален COM порт постоянно, никога не се преконфигурира.
- `client/session.py` (`RadioSession`) — държи живите връзки към
  текущо избраното радио + лек status-only контрол канал към ВСЯКО
  радио от списъка (за да показва "свободно"/"заето от X" за всички в
  списъка, не само избраното).
- Голям PTT бутон/индикатор.
- `client/settings_dialog.py` — отделен екран: CW източник (прав ключ /
  iambic paddle / WinKeyer), WPM, RC-28 вкл/изкл — не е на основния
  екран, точно както планът иска.
- Двата примерни клиентски конфига (`config.json`=ivan,
  `config.ic746.json`=georgi) вече изброяват и двете радиа всеки, с
  различни локални портове, за да могат едновременно да тестват
  multi-user сценария от по-ранните фази.

## Какво добавя Фаза 5 към Фаза 4

ACOM 1200S/eBox усилвателна интеграция. **Изследователската задача от
плана** (HTTP capture с Chrome DevTools на реален eBox) не мога да
направя тук — нямам устройството. Вместо да позная байтове, изтеглих и
прочетох реалния код на **bjornekelund/ACOM-Controller**
(github.com/bjornekelund/ACOM-Controller) — активно поддържан
open-source проект точно за тези усилватели, и същия протокол,
кръстосано потвърден от втори независим проект (`pingpongshow/
AcomControl`). Затова:

- `common/acom_protocol.py` — **потвърден, не гадан** RS-232 протокол:
  команди (standby/operate/off/telemetry вкл/изкл), 72-байтов telemetry
  frame с checksum, всички полета (мощност/reflected/SWR/температура/
  фен/band/грешка). Self-check с ръчно построен valid frame.
- `server/ebox_transport.py` — три транспорта:
  - `SerialAcomTransport` — директен RS-232 (същата връзка като
    ACOM-Controller), ако усилвателят е закачен направо.
  - `RawTcpAcomTransport` — **същия потвърден протокол** през суров TCP
    към IP-то на eBox — разумно предположение (много такива Ethernet-
    serial мостове просто препращат байтовете прозрачно), но
    непотвърдено срещу реален eBox. Пробвай това първо.
  - `HttpEboxTransport` — **изрично `NotImplementedError`** с точни
    инструкции какво трябва да направиш (DevTools capture), а не
    фалшива имплементация с измислени endpoint-и.
- `server/amplifier_bridge.py` — свързва transport, декодира telemetry,
  standby/operate/off, периодично логва в PostgreSQL. **Тестван
  end-to-end през реален TCP socket** срещу фалшив "усилвател" в
  self-check-а — не само чиста логика, а истински async I/O път.
- CAT mirror: `RadioBridge.add_data_observer()` (нов hook) захранва
  `AmplifierBridge.mirror_cat()` с копие на CAT потока на свързаното
  радио.
- Safety lockout: ако усилвателят докладва грешка/overtemp
  (`error_code != 0xff`), PTT на свързаното радио се отказва с ясна
  причина.
- Admin панел: нова секция "Усилватели" — CRUD + Operate/Standby/Off
  бутони на живо + текущи показания.

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

com0com виртуална двойка портове (по ЕДНА на клиент, не на радио —
Фаза 6 пренасочва я автоматично при превключване) все още се инсталира
ръчно — packaging е Фаза 7.

## Конфигурация

1. Пусни `python -m server.list_devices`, за да видиш реалните VID/PID/
   сериен номер на CAT адаптерите и `endpoint_id` на аудио устройствата
   (или ползвай dropdown-ите direct в admin панела — виж по-долу).
2. Първо стартиране: `server/config.json` — списък `"radios"` — служи
   само като seed. Ако `db.dsn` е зададен и таблицата `radio_configs` е
   празна, сървърът я зарежда там еднократно; след това конфигурацията
   се управлява през admin панела, не през config.json.
3. **PostgreSQL** — задължителен за admin панела (login, radio/amp CRUD,
   логване). Без него bridge-овете пак тръгват от `config.json`, но
   панелът показва инструкции вместо login форма.
   1. Инсталирай PostgreSQL, ако нямаш: postgresql.org/download (или
      "Install PostgreSQL" task-а в server инсталатора, ако е даден
      redist — виж `packaging/installer/redist/README.md`).
   2. Създай база: `createdb -U postgres hamradio` (или
      `psql -U postgres -c "CREATE DATABASE hamradio;"`).
   3. В `server/config.json` (до .exe-то, не в repo-то, ако си инсталирал
      през setup.exe) задай:
      ```json
      "db": { "dsn": "postgresql://postgres:ТВОЯТА_ПАРОЛА@localhost:5432/hamradio" }
      ```
      Формат: `postgresql://потребител:парола@host:порт/база`.
   4. Рестартирай сървъра — схемата (таблиците) се прилага автоматично
      при всяко стартиране, няма ръчна `psql -f schema.sql` стъпка.
   5. Отвори `http://<server-ip>:8080/` — при празна база те праща
      направо към `/setup`, за да създадеш първия admin от браузъра
      (алтернатива: `python -m server.create_admin ivan парола123` от
      команден ред).
4. `client/config.json` (ivan) / `client/config.ic746.json` (georgi) —
   всеки изброява ВСИЧКИ радиа в `"radios"`, с различни локални портове
   (com0com порт, аудио/CW UDP), за да могат двата примерни клиента да
   тестват едновременно на една машина. Сървър адрес, CW източник (прав
   ключ/iambic/WinKeyer) и RC-28 се сменят от "Настройки" екрана в
   самия клиент, не ръчно във файла.

## Стартиране

```bash
# сървър (CAT/аудио bridge-ове + admin панел на :8080)
python -m server.main

# клиенти
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

## Тест на превключването между радиа (Фаза 6)

Стартирай сървъра с двете радиа, после един клиент. Радио списъкът горе
трябва да покаже и двете с "свободно"/"заето". Кликни второто радио —
статусът минава на "Свързване..." после "Свързан", CAT/аудио/CW
автоматично сочат към новото радио. Ако имаш WSJT-X отворен на локалния
COM порт през цялото време, той никога не се докосва — само сървърният
край на връзката се сменя.

Headless проверка (без реален дисплей, за самия UI слой):
```bash
QT_QPA_PLATFORM=offscreen python -m client.main
```

## Тест на CW (без хардуер)

Клиентският CW панел (прав ключ бутон + текст→CW поле) работи с
клавиатура/мишка, без специален хардуер — задръж "CW ключ" или прати
текст, провери в server логовете, че `cw_method.set()` (CI-V или RTS/DTR,
според конфигурацията) реално се вика. За paddle/WinKeyer/RC-28 трябва
реалният хардуер на потребителя — виж бележките по-горе.

## Тест на усилвателя (без реален eBox)

1. Задай в admin панела нов усилвател с `transport: tcp`, IP на eBox,
   и (по избор) `linked_radio`.
2. Ако eBox прозрачно препраща байтовете — статусът в панела трябва да
   се напълни (мощност/SWR/температура) и Operate/Standby/Off бутоните
   да работят реално.
3. Ако не — `server/amplifier_bridge.py` ще логне грешка и продължи
   (не чупи останалата част от сървъра), и трябва да минеш през
   `HttpEboxTransport`-а: DevTools capture на реалния eBox, после
   попълни `connect()`/`write()` там.

## Самопроверки (без хардуер/база)

```bash
python -m server.ptt_arbiter
python -m server.device_registry
python -m server.db
python -m server.radio_bridge
python -m server.amplifier_bridge
python -m common.civ
python -m common.morse
python -m common.acom_protocol
python -m client.cw.iambic
python -m client.rc28
python -m common.updater
python -m common.audio_io
```
