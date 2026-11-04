# Локальный контур мониторинга Uptime Kuma

Два независимых Compose-проекта: `uptime-app` (Kuma 2.0.2 + MariaDB 11.4.5)
и `uptime-monitoring` (Prometheus, Grafana, cAdvisor, node-exporter,
mysqld-exporter, blackbox-exporter). Их соединяет внешняя сеть `uptime-metrics`.
У каждого проекта свои постоянные тома. База и экспортёры не публикуют порты.

## Запуск

Нужны rootful Docker Engine с cgroup v2, Dagger CLI **0.21.10** и Python >=3.11.
Проверенная среда: Windows + Docker Desktop 4.98 / Engine 29.9.2 + Ubuntu WSL2.
В Docker Desktop включите WSL Integration для Ubuntu. Команды выполняются
**в Ubuntu**, из каталога проекта в её Linux-файловой системе, например
`~/uptime-platform`. На `/mnt/c` настройка прав `.env` может не работать.

```bash
python3 scripts/init.py
# Команды Dagger с явным предоставлением доступа к сокету и секрету:
dagger call deploy-app --socket=/var/run/docker.sock --credentials=file:.env
dagger call deploy-monitoring --socket=/var/run/docker.sock --credentials=file:.env
dagger call configure-grafana --socket=/var/run/docker.sock --credentials=file:.env
# Или всё последовательно с проверкой:
dagger call deploy-all --socket=/var/run/docker.sock --credentials=file:.env
```

`.env` создаётся один раз со случайными паролями (48 hex-символов).
Существующий файл не перезаписывается. Не меняйте его после создания БД/Grafana:
смена файла сама по себе не ротирует пароли существующих пользователей.

Сокет и Secret передаются из вызывающего CLI: модуль не использует недоступный
`dag.host()` и не пытается читать пароли из своего исходного кода. Поэтому
**оба флага обязательны**, даже для команд configure/validate. Простая команда
без флагов завершается понятной ошибкой. `.env` исключён из исходников и
Directory-аргумента, не входит в архив. Ввод паролей в командную строку не нужен.

Открыть в Windows:

- [Kuma](http://127.0.0.1:3001) — при первом входе создать администратора.
- [Grafana dashboard](http://127.0.0.1:3000/d/uptime-health) — `admin`, пароль
  `GRAFANA_PASSWORD` из `.env`.
- [Prometheus](http://127.0.0.1:9090/query).

HTTP-проверка Kuma включает страницу первой настройки. Пользовательские
мониторы внутри Kuma автоматически не создаются.

## Архитектура

Kuma получает `/app/data/db-config.json` с параметрами MariaDB; это настоящий
внешний SQL backend, а не SQLite. MariaDB хранит данные в `db-data`, Kuma —
файлы в `kuma-data`. Зависимость `service_healthy` и healthchecks обеспечивают
порядок запуска.

Экспортёры:

| Компонент | Метрики |
| --- | --- |
| cAdvisor 0.55.1 | CPU, RAM, network контейнеров с метками Compose |
| node-exporter | CPU, RAM, файловые системы Linux-среды |
| mysqld-exporter | SQL connection, connections и другие метрики MariaDB |
| blackbox-exporter | HTTP доступность и latency Kuma/Grafana |

Prometheus собирает данные каждые 5 секунд, хранит 15 дней. Grafana использует
стабильный UID datasource `prometheus`, UID dashboard `uptime-health`.
`configure-grafana` через GET/POST/PUT API обновляет объекты без дубликатов.
Общий статус — произведение SQL и HTTP health. При исчезновении серии
`or vector(0)` даёт красный статус. Состояние scrape targets показано отдельно.

Файлы конфигурации записываются через stdin helper-контейнеров в именованные
тома **целевого Docker-демона**. Bind mounts путей рабочей папки Dagger не
используются. HTTP к Grafana/Prometheus выполняется helper-контейнером в сети
хостового Docker: имена сервисов не подменяются именами в сети Dagger.

Уникальный nonce исключает повторное использование закэшированных deploy/chaos.
Деплой пересоздаёт контейнеры, чтобы перечитать конфигурацию, и вызывает краткий
простой; тома сохраняются. Ошибки Docker выводятся с редактированием паролей.
Не выполняйте deploy/chaos одновременно.

## Docker Desktop и Linux

Рабочие настройки cAdvisor закреплены в генераторе: `pid: host`, `cgroup: host`,
`--containerd=/proc/1/root/run/containerd/containerd.sock`. Сокет читается через
PID namespace Docker-хоста, поэтому mount `/var/run` из Ubuntu не скрывает его.
Отдельный mount `/sys` исключён, чтобы он не подменял дерево cgroup деревом Ubuntu.
`rslave` у node-exporter исключён: корневой mount WSL не поддерживает этот режим.

Метрики хоста относятся к Linux/WSL, не к Windows целиком. cAdvisor может выдавать
предупреждения о machine-id и filesystem usage на Docker Desktop с overlayfs;
CPU/RAM/network проверяются отдельно, filesystem usage контейнеров не является
критерием приёмки. Для обычного Linux проверьте путь containerd: если он отличается,
измените `command` cAdvisor в `scripts/build.py` и повторите генерацию. Rootless
Docker и нестандартные data-root не проверены.

Конфигурации генерируются командой:

```bash
python3 scripts/build.py
```

JSON-файлы Compose являются валидным YAML-подмножеством.

## Проверки и chaos

```bash
python3 -m unittest discover -s tests -v
# Без пересоздания контейнеров:
dagger call validate --socket=/var/run/docker.sock --credentials=file:.env
# SQL и файл Kuma должны пережить пересоздание контейнеров:
dagger call persistence-check --socket=/var/run/docker.sock --credentials=file:.env
# Авария и автоматическое восстановление Docker:
dagger call chaos --target=kuma --mode=crash --socket=/var/run/docker.sock --credentials=file:.env
dagger call chaos --target=db --mode=crash --socket=/var/run/docker.sock --credentials=file:.env
# Буквальный docker stop, затем явный docker start:
dagger call chaos --target=db --mode=stop --socket=/var/run/docker.sock --credentials=file:.env
# Полная последовательная приёмка с логами:
python3 scripts/acceptance.py
# После исправления ошибки можно продолжить с первого непрошедшего шага:
python3 scripts/acceptance.py --resume
```

`validate` проверяет все 8 контейнеров, healthchecks, шесть scrape targets,
HTTP probes, SQL, метрики хоста и CPU/RAM каждого Compose-сервиса.
`persistence-check` записывает уникальную SQL-строку и файл Kuma, пересоздаёт
контейнеры, проверяет оба значения и удаляет маркеры. Таблица
`platform_persistence_check` остаётся пустой в БД; пользовательские записи не
изменяются.

В режиме `crash` команда замораживает **весь контейнер** через `docker pause`,
ждёт красного статуса, размораживает и посылает SIGKILL процессу через временный helper с host PID namespace.
Helper проверяет принадлежность PID целевому контейнеру по cgroup перед сигналом.
Вызов `docker kill` здесь не используется: на проверенном Docker он считается
ручной остановкой и не активирует restart-policy. Затем тест проверяет
увеличение RestartCount и возврат health к 1. Остановка только PID 1 не подходит:
в Kuma это dumb-init, а дочерний Node мог бы продолжать обслуживать HTTP.
`finally` размораживает и запускает контейнер при провале проверки.

После ручного `docker stop` restart-policy не восстанавливает сервис сама,
поэтому режим `stop` явно вызывает `docker start`. Оба сценария проверяют
1 → 0 → 1 по данным того же Grafana datasource и сохранённого дашборда, печатают
JSON с timestamps. Это API-проверка, а не screenshot-тест интерфейса.

Полная приёмка сохраняет `evidence/summary.json` и отдельный лог каждого шага.
Итоговые результаты перечислены в `VALIDATION.md`.

## Сохранность и ограничения

`unless-stopped` восстанавливает процессы после аварий, но один хост и одна БД
не дают HA при потере хоста/диска. Мониторинг независим от остановки приложения,
но зависит от общего Docker-хоста. Galera и HAProxy не используются.

Тома MariaDB, Kuma, Grafana, Prometheus переживают пересоздание контейнеров.
`docker compose down` сохраняет их; **`down -v` удаляет данные**. Внешние config-тома
и сеть не удаляются автоматически. Тома не заменяют резервные копии.

Порты доступны только через loopback, Grafana требует входа. Сокет Docker
эквивалентен административному доступу; cAdvisor запускается privileged и
использует host PID namespace. Пароли защищены Dagger Secret при передаче,
но администратор Docker может прочитать их в контейнерах/config-томах.
Образы фиксированы тегами, а не digest.

Если Windows возвращает `Empty reply` на localhost, сначала сравните запрос
внутри контейнера с запросом Windows; не связывайте это автоматически с VPN.
Перезапуск Docker Desktop может восстановить проброс портов; данные сохраняются.

Источники: [Dagger Unix sockets](https://docs.dagger.io/0.21/extending/arguments/#unix-sockets),
[Kuma database](https://github.com/louislam/uptime-kuma/blob/2.0.2/server/database.js),
[cAdvisor containerd endpoint](https://github.com/google/cadvisor/blob/v0.55.1/container/containerd/factory.go),
[Compose cgroup](https://docs.docker.com/reference/compose-file/services/#cgroup).
