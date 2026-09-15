# HAM Radio Remote — Фаза 1 прототип

Клиент-сървър дистанционно управление на IC-7300: CAT bridge (виртуален
COM порт → мрежа → реален CAT сериен порт) + двупосочно аудио
(RTP-стил UDP + Opus). Един потребител, едно радио. Виж
[ham-radio-remote-plan.md](ham-radio-remote-plan.md) за пълния план по фази.

## Структура

```
common/audio_io.py   споделен mic/speaker <-> UDP/Opus link (server + client)
common/civ.py         CI-V PTT байтове
server/cat_bridge.py  сериен <-> TCP relay
server/main.py         сървър entry point
server/db/schema.sql   PostgreSQL структура (за Фаза 2, не се ползва още)
client/com_relay.py   com0com виртуален порт <-> TCP relay
client/ui.py            минимален PySide6 UI
client/main.py         клиент entry point
```

## Инсталация

На сървърната машина (там, където е включено IC-7300):

```bash
pip install -r server/requirements.txt
```

На клиентската машина:

```bash
pip install -r client/requirements.txt
```

Клиентът очаква вече създадена com0com виртуална двойка портове (напр.
COM10↔COM11) — инсталирай com0com отделно (packaging/auto-install е
Фаза 7). CAT софтуерът (WSJT-X и др.) се сочи към единия край
(COM10), нашият клиент отваря другия (COM11).

## Конфигурация

Редактирай `server/config.json` (сериен порт на IC-7300, аудио
устройства) и `client/config.json` (com0com порт, IP на сървъра).
`audio.input_device`/`output_device`: `null` за системното устройство
по подразбиране, или име/индекс от `python -m sounddevice`.

## Стартиране

```bash
# на сървъра
python -m server.main

# на клиента
python -m client.main
```

## Тест

1. Стартирай сървъра, после клиента — статусът в клиента трябва да
   стане "Свързан".
2. Отвори WSJT-X (или друг CAT софтуер) на COM10 — трябва да чете
   честотата от радиото през bridge-а.
3. Задръж PTT бутона в клиента — радиото трябва да превключи на
   предаване (провери индикатора на IC-7300).
4. Говори в микрофона на клиентската машина — звукът трябва да се чуе
   от аудио изхода на сървърната машина (и обратно, звук от радиото →
   високоговорител на клиента).

## Самопроверки

```bash
python -m common.civ
```
