# Online Cinema: Event Streaming + Analytics Pipeline

Полная реализация задания на 10 баллов: Movie Service → Kafka + Schema Registry →
ClickHouse Kafka Engine → Aggregation Service → PostgreSQL → S3, Grafana читает
материализованные агрегаты ClickHouse. Python 3.13, FastAPI, uv, asyncpg, Avro.

## Запуск

```bash
docker compose up --build
# либо в фоне, с ожиданием health checks:
make up
```

Копировать `.env.example`, создавать топики, регистрировать схемы, применять SQL,
создавать бакет или импортировать дашборд вручную не требуется. При первом старте
Docker загружает довольно крупные образы. Нужны Docker Compose v2, около 8 ГБ
свободной RAM и 12 ГБ диска. Остановить: `make down`; данные сохраняются в volumes.

| Компонент | Адрес / доступ |
|---|---|
| Movie Service Swagger | http://localhost:18180/docs |
| Aggregation Service Swagger | http://localhost:18181/docs |
| Grafana dashboard | http://localhost:13000/d/cinema-analytics |
| Grafana | `admin` / `cinema-grafana-local` |
| Schema Registry | http://localhost:18085/subjects/movie-events-value/versions |
| ClickHouse HTTP | http://localhost:18123, `cinema` / `cinema-local` |
| PostgreSQL | localhost:15434, БД/пользователь `cinema`, пароль `cinema-local` |
| S3 API (SeaweedFS) | http://localhost:19000 |
| S3 credentials | `cinema-s3` / `cinema-s3-local`, бакет `movie-analytics` |

Порты можно переопределить через `.env` (см. `.env.example`). Учебные пароли
заданы явно; опубликованные порты привязаны к loopback. Kafka доступна внутри
Compose-сети. Имена сервисов и advertised listeners совпадают.

SeaweedFS — разрешённый заданием S3-совместимый аналог MinIO. Экспорт использует
стандартный `boto3` S3 API, формат и путь объектов не зависят от реализации S3.

## Проверка одной командой

```bash
make test       # сборка, инфраструктура, bootstrap и все тесты в контейнере
make demo       # видимая HTTP → Kafka → ClickHouse → PG → S3 демонстрация
make faults     # отключение брокера, ClickHouse, S3 и PostgreSQL, проверка восстановления
make lint       # uv + Ruff
make unit       # быстрые тесты без Docker
make sql        # SELECT из PostgreSQL
```

`make test` поднимает зависимости автоматически. Проверяется реальная цепочка,
без mock Kafka/ClickHouse/S3. Тесты выбирают свободное историческое окно,
используют уникальные user/event IDs, затем удаляют только свои события,
агрегаты и S3-объекты. Можно запускать повторно на заполненной системе.
`make faults` останавливает только компоненты этого Compose-проекта и возвращает
их в работу в `finally`; он доказывает и автоматический повтор экспорта после сбоя.
GitHub Actions воспроизводит lint, тесты, демонстрацию и fault checks.

## Соответствие заданию

| Баллы | Реализация |
|---|---|
| 1 | Avro v1/v2, FULL_TRANSITIVE, topic с 3 partitions, RF=3, min ISR=2 |
| 2 | FastAPI JSON API, schema validation, acks=all, idempotent producer, retry/backoff, JSON logs, генератор |
| 3 | Kafka Engine AvroConfluent → Materialized Views → MergeTree, quarantine ошибок |
| 4 | Реальный интеграционный тест HTTP → Kafka → ClickHouse с проверкой всех полей |
| 5–7 | Отдельный scheduled Aggregation Service, дневные SQL-метрики, CH snapshots, атомарная идемпотентная запись PostgreSQL |
| 8 | Provisioned Grafana ClickHouse datasource, cohort heatmap Day0–7 и ещё 5 панелей, все запросы к агрегатам |
| 9 | Автоматический/ручной S3 JSON export из PG, partitioned path, overwrite, durable retry queue |
| 10 | 3 Kafka brokers/controllers, replicated topic, Schema Registry, health checks, broker failover test |

## События и API

`POST /events` возвращает **201 только после delivery callback Kafka**:

```bash
curl -sS http://localhost:18180/events \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id":"alice",
    "movie_id":"movie-01",
    "event_type":"VIEW_STARTED",
    "timestamp":"2026-01-01T12:00:00Z",
    "device_type":"TV",
    "session_id":"alice-session-1",
    "progress_seconds":0
  }'
```

Ответ: `{"event_id":"<uuid>","partition":1,"offset":42}`. UUID можно передать
самостоятельно: при повторе запроса после неопределённого результата нужно
использовать **тот же event_id и то же содержимое**. Без UUID сервер создаст новый.
Невалидный JSON/поля: 422 с путями ошибок; недоступная Kafka после retries: 503.

Поля соответствуют Avro. `timestamp` обязан содержать timezone, нормализуется
в UTC с миллисекундной точностью, допускает исторические данные с 2000 года,
но не более 5 минут в будущем. UUID, enum, размеры строк, целое неотрицательное
`progress_seconds` проверяются до публикации; просмотр требует progress,
VIEW_STARTED требует 0. SEARCHED/LIKED могут иметь null progress. Неизвестные поля
запрещены. Avro-валидация повторяется непосредственно перед сериализацией.

`schemas/movie-event-v1.avsc` содержит обязательные поля задания. В v2 добавлен
nullable `app_version` с default=null — совместимая эволюция. Bootstrap
идемпотентно регистрирует **версии 1 и 2** и проверяет их содержимое. Продюсер
загружает закреплённую v2 из Registry и сверяет с исходником. Сообщение — настоящий
Confluent Avro wire format (magic byte, schema ID, binary Avro), не JSON в Kafka.
Тест проверяет чтение v1 и v2 через один Kafka Engine.

**Ключ — user_id**: события одного пользователя попадают в одну partition,
сохраняют порядок записи и не дробят его сессии между consumers. Это удобнее для
retention/DAU, чем movie_id, и избегает концентрации на одном популярном фильме.
Kafka гарантирует порядок записи внутри partition, а не порядок timestamp
параллельных HTTP-запросов. Аналитика группирует по event time в UTC.

По умолчанию генератор сначала наполняет 10 исторических когорт, включая D1–D7,
потом непрерывно генерирует сессии. SEARCHED → VIEW_STARTED → VIEW_PAUSED →
VIEW_RESUMED → VIEW_FINISHED → LIKED; часть сессий не завершается. Прогресс
не убывает, pause/resume имеют одинаковый progress, timestamps растут. Для
исторического seed UUID детерминированные: перезапуск не увеличивает метрики.
`GENERATOR_ENABLED=false` отключает поток, `DEMO_SEED_ENABLED=false` — историю.
API и генератор работают одновременно. Исторические графики появляются после
доставки seed и следующего цикла агрегации.

## Хранение и гарантии доставки

`raw_events`: месячные partitions по event timestamp, ORDER BY
`(event_date, user_id, event_id)` для дневных выборок и пользовательской активности.
Сохраняются Kafka partition/offset и ingestion timestamp для диагностики.
Kafka Engine имеет 3 consumers, берёт Avro-схему по ID из Registry и складывает
записи через MV. Ошибки декодирования отправляются во вторую таблицу
`ingestion_errors`, чтобы одно повреждённое сообщение не блокировало поток.

Доставка ingestion — **at least once**. `enable.idempotence=true` исключает
дубли внутренних producer retries, но не повторного HTTP-запроса или Kafka
Engine replay. Аналитическое представление `cinema.events` устраняет повторы
**по event_id до всех агрегаций**, с детерминированным выбором самой ранней записи.
Это работает без ожидания фоновых MergeTree merges и между месячными partitions.
Повторное использование UUID с другим содержимым — нарушение контракта клиента:
сохраняется самая ранняя запись, изменения должны иметь новый event_id.

## Метрики: определения

Каждый ручной или scheduled расчёт читает `cinema.events` напрямую из ClickHouse,
использует единый ingestion cutoff и SQL `uniqExact`, `avgOrNullIf`, `countIf`,
`min`, `GROUP BY`, joins. Метрики рассчитываются за UTC-день.

| Метрика | Определение |
|---|---|
| DAU | Уникальные user_id с любым событием за день |
| Среднее время просмотра | Среднее progress_seconds у VIEW_FINISHED; без завершений null |
| Топ фильмов | Количество VIEW_STARTED каждого movie_id, рейтинг в Grafana |
| Конверсия | VIEW_FINISHED / VIEW_STARTED за день, null при нулевом знаменателе |
| Retention D1/D7 | Когорта первого VIEW_STARTED; доля вернувшихся с VIEW_* ровно через 1/7 UTC-дней |
| Cohort heatmap Day0–7 | Когорта первого события любого типа; доля с любым событием в соответствующий день |
| Device users | Уникальные пользователи каждого device_type за день |

Различие first view / first activity намеренное: оба определения присутствуют
в задании. Проценты хранятся как доли 0..1, Grafana форматирует их в %. Будущие и
ещё не завершившиеся дни retention — null (пустые ячейки), зрелые дни без возвратов
— 0. Для пустой когорты retention null; размер когорты хранится отдельно.
Конверсия по календарному дню может превышать 100%, если в этот день закончились
просмотры, начатые раньше: формула соответствует заданию, искусственного clamp нет.

Ручной пересчёт/экспорт (дата — от 2000-01-01 до сегодня):

```bash
curl -sS -X POST http://localhost:18181/aggregations/2026-01-01
curl -sS 'http://localhost:18181/metrics?metric_date=2026-01-01'
curl -sS -X POST http://localhost:18181/exports/2026-01-01
```

Scheduler запускается сразу при старте и затем через
`AGGREGATION_INTERVAL_SECONDS` после завершения предыдущего цикла (default 60).
Каждый цикл пересчитывает сегодня и предыдущие `AGGREGATION_LOOKBACK_DAYS` дней
(default 9, минимум 8), чтобы D1/D7 когорт обновлялись после новых возвратов и
обычных задержек доставки. Для событий старше окна нужно вызвать ручной backfill
затронутых дней/когорт. Логи содержат start/end, date, processed_events, metrics,
duration_ms; ошибки отдельных дат не останавливают остальные даты или scheduler.

## Идемпотентность и восстановление

ClickHouse хранит `metric_snapshots` с run_id и `aggregation_commits`. Завершённый
снимок становится виден в `daily_metrics` только после commit marker; view
выбирает целиком последний run каждой даты. Поэтому повторный расчёт не
дублирует показатели и не оставляет старые movie/device dimensions.

В PostgreSQL `daily_metrics` имеет PK `(metric_date,metric_name,dimension)`, value,
computed_at, run_id. Полная замена даты и запись aggregation_runs/export_jobs —
одна транзакция. PostgreSQL advisory lock сериализует пересчёты и экспорты между
несколькими экземплярами сервиса. Потеря соединения освобождает lock.

Распределённой транзакции между CH и PG нет: CH snapshot публикуется первым,
затем PG. Если PG запись не удалась, retry повторяет весь расчёт; успешный повтор
сводит обе БД к новому снимку, PG никогда не содержит половину даты. Временное
расхождение двух хранилищ при сбое возможно и не выдаётся за exactly once.

После каждого расчёта дата ставится в долговечную `export_jobs`. Scheduler
экспортирует завершённые дни, включая старые неудавшиеся задания вне lookback.
Экспорт читает только PG, включает все метрики/даты/computed_at и пишет
`s3://movie-analytics/daily/YYYY-MM-DD/aggregates.json`. Один ключ перезаписывается;
null сохраняется JSON null, NaN не допускается. Экспорт и замена снимка
сериализованы, поэтому медленная старая выгрузка не затрёт новую. Сбой после S3
PUT, но до отметки в PG безопасен: следующий запуск перезапишет тот же объект.
S3 недоступен: retries/backoff, лог, 503 ручному клиенту, pending job остаётся
до следующего цикла. PostgreSQL недоступен: расчёт/экспорт повторяются после
восстановления, процессы продолжают работать. S3 не блокирует health агрегации.

Kafka — 3 combined KRaft brokers/controllers, replication.factor=3,
min.insync.replicas=2, producer acks=all. Остановка одного узла сохраняет quorum
контроллеров и две ISR, чтение/запись продолжаются. Отказ двух узлов останавливает
запись вместо подтверждения ненадёжной доставки. Вся Kafka-история сохраняется
7 дней; после перезапуска ClickHouse догоняет по offsets. Это учебный кластер
на одном Docker-хосте, а не защита от потери всего хоста.

## Миграции и структура

Bootstrap после health checks создаёт topic/схемы, применяет версионированные
SQL-миграции с журналом и SHA-256 checksum, создаёт S3 bucket и Grafana user.
PG migrations транзакционные и защищены advisory lock. CH DDL идемпотентен:
после частичного сбоя выполняется повторно, версия отмечается после всех DDL.
Редактирование уже применённой миграции обнаруживается и блокирует запуск.
Для развития добавляется новый файл, а не меняется применённый.

```text
app/producer/           HTTP, Avro serialization, Kafka delivery, generator
app/analytics/          ClickHouse SQL, aggregation, scheduler, durable S3 export
app/common/            config, async HTTP ClickHouse, retry, structured logs
app/bootstrap.py       automatic setup/migrations
schemas/               versioned Avro contracts
migrations/            PostgreSQL and ClickHouse migrations
deploy/               Kafka-adjacent configs, S3 auth, Grafana provisioning
scripts/               live E2E demonstration and real outage checks
tests/                 validation, wire compatibility, metrics, integration
```

Grafana plugin закреплён в Dockerfile и устанавливается при сборке. Datasource и
6 панелей provisioned из Git; read-only ClickHouse user имеет SELECT только на
агрегатное view и две лежащие под ним таблицы снимков, без доступа к raw. Cohort panel — таблица с цветным фоном ячеек, колонками Day0–7 и
процентами, не histogram heatmap. Интеграционный тест исполняет **каждый запрос
через Grafana datasource API**, проверяя plugin, credentials и macro expansion.

Схема связей и границы отказов: [docs/architecture.md](docs/architecture.md).
