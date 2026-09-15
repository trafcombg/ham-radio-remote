# HAM Radio Remote — Фаза 1 + Фаза 2

Клиент-сървър дистанционно управление на Icom радиостанции: CAT bridge
(виртуален COM порт → мрежа → реален CAT сериен порт) + двупосочно аудио
(UDP + Opus) + PTT арбитраж между потребители. Виж
[ham-radio-remote-plan.md](ham-radio-remote-plan.md) за пълния план по фази.

## Какво добавя Фаза 2 към Фаза 1

- Второ радио (IC-746PRO през microHAM, PTT през RTS/DTR линия вместо CAT)
- PTT методът е абстрахиран (`server/radio_bridge.py`: `CivPtt` / `LinePtt`), конфигурируем per радио
- Стабилна device идентификация по VID/PID/сериен номер/USB път (`server/device_registry.py`) вместо по COM име/индекс
- Отделен control канал (login + PTT заявка/отговор + статус) — PTT вече не минава като сурови CAT байтове от клиента, защото арбитражът трябва да го прихване преди да стигне до радиото, а RTS/DTR изобщо не са CAT данни
- PTT lock per радио (`server/ptt_arbiter.py`) — втори потребител на заето радио бива отказан, не опашкуван
- Session/transmission логване в PostgreSQL (`server/db.py`) — по избор, работи и без база (`NullDb`)

## Структура (нови/променени спрямо Фаза 1)

```
server/device_registry.py  VID/PID/serial/location -> COM порт / аудио индекс, + device watcher
server/list_devices.py     помощна CLI: показва наличните устройства и техните стабилни ID
server/ptt_arbiter.py      lock: кой потребител предава в момента, per радио
server/db.py                 PostgreSQL логване (NullDb fallback без конфигурирана база)
server/radio_bridge.py     оркестрира едно радио: CAT relay + control канал + аудио + PTT
server/main.py               зарежда списък радиа, резолва устройства, стартира bridge-овете
client/control.py           login + PTT заявка/отговор + статус "заето от X"
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
2. Попълни `server/config.json` — списък `"radios"`, по един запис на
   радио. За идентификация по VID/PID: попълни `cat.vid`/`cat.pid` (и
   `serial_number`/`location` ако две устройства споделят VID/PID — при
   двусмислие сървърът отказва да стартира точно това радио и логва
   грешка, вместо да гадае). Или просто задай `cat.serial_port` директно,
   ако не искаш VID/PID резолюция.
3. `ptt.method`: `"civ"` (IC-7300 — PTT през CI-V команда) или `"rts"`/
   `"dtr"` (IC-746PRO+microHAM — PTT през серийна линия). `ptt.serial_port:
   null` означава "използвай същия сериен порт като CAT" (типично за
   microHAM интерфейси).
4. По избор: `db.dsn` — PostgreSQL connection string за логване на
   сесии/предавания. Приложи схемата веднъж: `psql <dsn> -f server/db/schema.sql`.
   Без `dsn` логването просто е изключено (`NullDb`), останалото работи.
5. `client/config.json` (радио 1) и `client/config.ic746.json` (радио 2,
   различен потребител) — попълни `com.local_port` с твоя com0com порт.

## Стартиране

```bash
# сървър
python -m server.main

# клиент 1 (IC-7300, потребител "ivan")
python -m client.main

# клиент 2 (IC-746PRO, потребител "georgi") — на друга машина или втори прозорец
python -m client.main config.ic746.json
```

## Тест

**Multi-radio / multi-user:** стартирай сървъра, после двата клиента —
и двата трябва да покажат "Свързан", всеки контролира различно радио,
независимо аудио и CAT.

**PTT lock:** задръж PTT на клиент 1 — статусът му минава на "Предава".
Ако друг потребител се свърже КЪМ СЪЩОТО радио (втори control клиент на
същия control порт) и опита PTT, получава `ptt_denied` с ясна причина
("заето от ivan") вместо да ключва радиото — виж
`server/ptt_arbiter.py`'s self-check по-долу за логиката без хардуер.

**RTS/DTR PTT:** на IC-746PRO+microHAM, задръж PTT в клиента — микрофонният
вход на microHAM интерфейса трябва да се включи (провери индикатора на
интерфейса/радиото), без да е изпратена нито една CI-V команда.

## Самопроверки (без хардуер)

```bash
python server/ptt_arbiter.py
python server/device_registry.py
python common/civ.py
```
