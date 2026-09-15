# Пакетиране — Фаза 7

Проверено реално в тази среда: и двата `.spec` файла build-ват работещи
standalone .exe (сървърът реално сервира admin панела, клиентът реално
стартира Qt+asyncio); и двата `.iss` скрипта компилират чисто с Inno
Setup 6 в работещи `.exe` инсталатори. Не съм пускал самите инсталатори
(изискват admin elevation и модифицират системата) — компилацията им е
достатъчно доказателство за коректен синтаксис/структура.

## 1. Build на .exe (PyInstaller)

```bash
pip install pyinstaller -r server/requirements.txt -r client/requirements.txt
pyinstaller packaging/server.spec
pyinstaller packaging/client.spec
```

Резултат: `dist/HAM-Radio-Server/` и `dist/HAM-Radio-Client/`. И двата
четат `config.json` от **папката на .exe-то** (не от вътрешността на
bundle-а) — виж `common/app_paths.py`.

## 2. Build на инсталатор (Inno Setup)

Изисква [Inno Setup 6](https://jrsoftware.org/isinfo.php) (безплатен).
По избор: сложи `com0com-setup.exe`/`postgresql-setup.exe` в
`packaging/installer/redist/` (виж README-то там) за автоматична
инсталация на драйвера/базата.

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging/installer/server.iss
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging/installer/client.iss
```

Резултат: `dist_installers/HAM-Radio-Server-Setup.exe` и
`HAM-Radio-Client-Setup.exe`.

**Преди реално разпространение**, задължително провери/попълни:
- `#define MyAppPublisher` в двата `.iss` файла (твоя call sign/клуб)
- `GITHUB_REPO` в `common/updater.py` — реалното repo, откъдето ще идват
  releases
- Силните флагове за com0com/PostgreSQL инсталаторите — варират по
  версия, виж коментарите в `.iss` файловете и `redist/README.md`

## 3. Auto-update (`common/updater.py`)

- Проверява `GITHUB_REPO`'s latest release на GitHub API.
- **Клиент:** проверява на всеки 6 часа; при нова версия показва бутон
  "Обнови" в UI — при клик сваля новия инсталатор и го пуска
  `/VERYSILENT` (Inno Setup затваря running инстанция сам чрез
  `AppMutex`), после клиентът излиза.
- **Сървър:** проверява на всеки 24 часа; **автоматично сваля**
  новия инсталатор в temp, но НЕ го пуска сам — рестартиране на жив
  CAT/аудио/PTT сървър посред сесия е рисково. Логва пътя до сваления
  инсталатор, операторът го пуска ръчно когато е удобно.
- Публикувай releases в GitHub с tag `vX.Y.Z` и assets, съдържащи
  `Server-Setup`/`Client-Setup` в името на файла (за да съвпадне с
  `asset_name_contains` проверката).

## 4. Деинсталация

И двата `.iss` премахват всичко в `{app}` (`[UninstallDelete]`).
Сървърният инсталатор пита при деинсталация дали да запази PostgreSQL
(`[Code]` секцията в `server.iss`) — ако избереш "не", търси и пуска
собствения uninstaller на PostgreSQL (намерен по DisplayName в
registry, не хардкоднат ключ). Тази стъпка не е тествана срещу реална
PostgreSQL инсталация тук — провери преди да разчиташ на нея.

## Самопроверка

```bash
python -m common.updater
```
