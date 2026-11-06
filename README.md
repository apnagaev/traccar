# Traccar (Ansible + Docker Compose)

[Traccar](https://www.traccar.org/) — сервер GPS-мониторинга: телефоны и трекеры на карте, история поездок, геозоны, уведомления. Два узла с общей базой, вход через Keycloak, разворачивается и обновляется одним плейбуком.

- Traccar на двух узлах с общей MySQL/MariaDB, синхронизация через Redis; регламентные задачи — только на узле 1
- Вход только через Keycloak (OpenID Connect), доступ и права администратора — по группам
- Секреты не лежат в `traccar.xml`: Traccar берёт их из переменных окружения (`traccar.env`, 0600)
- tracksms — «SMS»-уведомления Traccar, которые уходят в Telegram-чаты по устройствам
- Обновление без простоя: узлы перезапускаются по очереди, каждый проверяется
- Firewall на nftables: веб, приём телефонов и tracksms доступны только reverse proxy и внутренней сети; порты трекеров открыты

```
      телефоны (HTTPS)        браузеры              трекеры (TCP/UDP)
            │                    │                         │
     ┌──────▼────────────────────▼──────┐                  │
     │ reverse proxy: geo.* → :15055    │  vIP             │
     │ track.* → :82, реальный IP → ip  │                  │
     └──────┬────────────────────┬──────┘                  │
            │                    │                         │
      ┌─────▼────┐        ┌──────▼───┐◄────────────────────┘
      │  node1   │        │  node2   │   traccar + tracksms
      │ основной │        │ secondary│
      └──┬────┬──┘        └──┬────┬──┘
         │    └───── Redis ──┘    │
         └──────► MySQL ◄─────────┘
```

## Структура

```
install_traccar.yaml            # плейбук: firewall, файлы, поочерёдный запуск, проверка, очистка
inventory.example.yml           # пример inventory (группа docker, node: 1/2)
vars/traccar.example.yml        # все настройки: версия, порты, каталоги, база, Keycloak, ключи, tracksms, firewall
tasks/traccar-healthcheck.yaml
files/traccar/
  traccar.yaml.j2               # docker-compose.yaml (traccar + tracksms)
  traccar.xml.j2                # конфигурация Traccar без секретов; на узле 2 — broadcast.secondary
  traccar.env.j2                # секреты переменными окружения (0600 на хосте)
  traccar.nft.j2                # правила nftables
  smsconf.txt.j2                # tracksms: порт и токен бота
  userdevices.txt.j2            # tracksms: устройство → телефон и чат
  templates/{ru,en}/*.vm        # свои шаблоны уведомлений (по одному файлу поверх штатных)
```

Исходники tracksms лежат на узлах в `tracksms_build_dir` (по умолчанию `/traccar/tracksms`) и в репозиторий не входят.

## Настройка

```bash
cp inventory.example.yml inventory.yml
cp vars/traccar.example.yml vars/traccar.yml
ansible-vault encrypt vars/traccar.yml
```

В `vars/traccar.yml`: версия, порты, база, Redis, Keycloak, ключи внешних сервисов, устройства tracksms, `trusted_nets` (сети reverse proxy и мониторинга).

Секреты попадают в контейнер переменными окружения: Traccar включает это по `CONFIG_USE_ENVIRONMENT_VARIABLES=true`, имя переменной — ключ конфигурации в верхнем регистре (`database.password` → `DATABASE_PASSWORD`, `openid.clientSecret` → `OPENID_CLIENT_SECRET`).

## Развертывание

```bash
ansible-playbook -i inventory.yml install_traccar.yaml --ask-vault-pass
```

На каждом узле по очереди:
1. Применяет nftables (`/etc/nftables.d/traccar.nft`).
2. Кладёт `docker-compose.yaml`, `traccar.xml`, `traccar.env` (0600) и файлы tracksms, удаляет файлы прежних версий плейбука.
3. Скачивает образ Traccar, собирает tracksms, выполняет `docker compose up -d`; если поменялись только `traccar.xml`, шаблоны уведомлений или файлы tracksms — перезапускает этот контейнер.
4. Ждёт веб-интерфейс, порты протоколов и tracksms, только потом переходит к следующему узлу.
5. Удаляет неиспользуемые образы (тома и сети других сервисов не трогает).

Журналы Traccar — в `traccar_logs_dir`, старше `traccar_logs_keep_days` дней удаляются по cron.

## Версии

По умолчанию `traccar_version: latest`: каждый прогон берёт свежий образ. Traccar обновляет схему общей базы при старте, поэтому узлы обновляются строго по очереди, первым — узел 1, второй стартует только после проверки первого. Можно закрепить конкретную версию (`6.17.0`).

С 6.17 порты протоколов при первом запуске записываются в базу; дальше `*.port` в `traccar.xml` их не меняют — порты меняются в разделе протоколов администратора.

## Шаблоны уведомлений

В Traccar 6.x шаблон — один файл на событие и язык: `templates/notifications/<язык>/<событие>.vm` с `$subject` (тема письма), `$digest` (короткий текст: SMS, Telegram, push) и HTML письма. Здесь лежат только изменённые шаблоны, каждый подключается в контейнер отдельным файлом, остальные остаются штатными.

Изменение одно: `$digest` начинается с `#deviceid<id устройства>` — по нему tracksms выбирает Telegram-чат (`userdevices.txt`). Для основных событий короткий текст свой (хештеги, адрес, ссылка на Яндекс Карты), для остальных событий с устройством к штатному тексту добавлен только `#deviceid<id>`. Тема и HTML писем — штатные (6.17.0); в `ru/deviceFuelDrop.vm` исправлена незакрытая кавычка в теме, из-за которой штатный шаблон не разбирается.

После изменения шаблонов плейбук перезапускает контейнер traccar.

## Reverse proxy

- Веб: `upstream` на `:82` обоих узлов, WebSocket (`/api/socket`).
- Телефоны (протокол OsmAnd): `upstream` на `:15055`. Traccar записывает в атрибут `ip` адрес соединения, то есть адрес прокси, поэтому `processing.remoteAddress.enable` выключен, а реальный IP телефона дописывает в запрос сам прокси (для OpenResty — Lua-блок `traccar-real-ip.conf`: JSON → `location.extras.ip`, параметры → `&ip=`). Порт `:15055` закрыт firewall: прямой доступ позволил бы подставить любой адрес.
- `status.timeout` (`traccar_status_timeout`, по умолчанию 3900 с) должен быть больше самого редкого интервала отправки телефонов на стоянке, иначе они большую часть времени в статусе «неизвестно».

## Подмена GPS: точки улетают в чужие зоны

При глушении и подмене GPS трекер отправляет точки в нескольких постоянных зонах (обычно у аэропортов), часто по кругу, хотя машина стоит. `tools/spoof_zones.py` находит эти зоны по истории (прыжок от настоящего места с невозможной скоростью, точки до возврата) и создаёт четыре вычисляемых атрибута с отрицательным приоритетом — они выполняются до фильтров:

1. `spoofed` — точка с GPS прыгнула дальше 1 км от прошлой со скоростью больше 350 км/ч, либо попала в зону подмены прыжком быстрее 150 км/ч или продолжает подмену, пока остаётся в зоне и далеко от прошлой точки;
2. `latitude`, `longitude` — у такой точки координаты прошлой точки: машина остаётся на месте;
3. `speed` — 0.

Настоящая поездка в зону не страдает: точки приходят постепенно, без прыжка. Нужна настройка `processing.computedAttributes.lastAttributes` (есть в `traccar.xml.j2`).

```bash
python3 tools/spoof_zones.py --url https://track.example.com --token TOKEN --days 60 --save zones.json   # зоны и проверка на истории
python3 tools/spoof_zones.py --url https://track.example.com --token TOKEN --zones zones.json --apply --group-id 1
```

Атрибуты привязываются к группе (или к устройствам) в «Настройки → Атрибуты». Пересчитать зоны позже — тот же запуск с `--apply`: атрибуты обновятся по описанию.
