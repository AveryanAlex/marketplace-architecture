# Схема данных и протокол согласования

## ER-диаграмма (Mermaid)

Две базы, без внешних ключей между сервисами. `Booking.flight_id` и
`SeatReservation.booking_id` — ссылки по контракту gRPC.

```mermaid
erDiagram
    FLIGHTS ||--o{ SEAT_RESERVATIONS : has
    FLIGHTS {
        uuid id PK
        text flight_number "UNIQUE with UTC departure day"
        text airline
        char3 origin "IATA"
        char3 destination "IATA, differs from origin"
        timestamptz departure_at
        timestamptz arrival_at "greater than departure_at"
        int total_seats "greater than 0"
        int available_seats "0..total_seats"
        bigint price_minor "positive kopecks RUB"
        flight_status status
    }
    SEAT_RESERVATIONS {
        uuid id PK
        uuid booking_id UK
        uuid flight_id FK
        int seat_count "1..1000"
        reservation_status status "ACTIVE / RELEASED / EXPIRED"
        timestamptz created_at
    }
    RELEASED_BOOKINGS {
        uuid booking_id PK
        timestamptz created_at
    }
    CACHE_REVISION {
        int id PK "singleton 1"
        bigint revision
    }
    BOOKINGS ||--o| CANCELLATION_JOBS : pending_cancel
    BOOKINGS {
        uuid id PK
        text user_id "1..128 chars"
        uuid flight_id "external reference"
        text passenger_name
        text passenger_email
        int seat_count "1..1000"
        bigint total_price_minor "immutable positive snapshot"
        booking_status status "CONFIRMED / CANCELLED"
        timestamptz created_at
    }
    BOOKING_ATTEMPTS {
        uuid id PK "future booking ID"
        text fingerprint "SHA256 canonical request"
        text state "PROCESSING / SUCCEEDED / COMPENSATING / COMPENSATED"
        timestamptz created_at
    }
    CANCELLATION_JOBS {
        uuid booking_id PK,FK
        timestamptz created_at
    }
```

`flights`, `seat_reservations`, `released_bookings`, `cache_revision` принадлежат
Flight PostgreSQL. Остальные таблицы — Booking PostgreSQL. Ограничения реализованы
в SQL, а не только в валидации HTTP/gRPC.

Все бизнес-атрибуты зависят от ключа своей сущности; повторяющихся групп и
транзитивных зависимостей между неключевыми бизнес-атрибутами нет. Данные пассажира
относятся к конкретному бронированию, отдельной сущности «профиль пользователя»
нет. Стоимость бронирования — исторический факт, не ссылка на изменяемую цену рейса.
Дата вылета не хранится отдельным атрибутом: уникальный expression index по
`flight_number` и `(departure_at AT TIME ZONE 'UTC')::date` обеспечивает
уникальность номера рейса в пределах дня без избыточного поля. Доступный остаток — транзакционно поддерживаемый
счётчик; его границы защищены CHECK и блокировкой рейса.

## Создание и компенсация

```mermaid
sequenceDiagram
    participant C as REST client
    participant B as Booking
    participant BD as Booking PostgreSQL
    participant F as Flight gRPC
    participant FD as Flight PostgreSQL
    C->>B: POST /bookings + optional Idempotency-Key
    B->>F: GetFlight (price snapshot)
    F-->>B: Flight
    B->>BD: COMMIT durable attempt PROCESSING
    B->>BD: BEGIN, lock attempt
    B->>F: ReserveSeats(booking_id, flight_id, count)
    F->>FD: BEGIN, lock booking key, SELECT flight FOR UPDATE
    F->>FD: decrement seats + INSERT reservation + bump cache revision
    F->>FD: COMMIT
    F-->>B: ACTIVE
    alt Booking INSERT succeeds
        B->>BD: INSERT booking CONFIRMED, attempt SUCCEEDED, COMMIT
        B-->>C: 201 Booking
    else RPC or local write fails
        B->>BD: ROLLBACK, persist compensation intent
        B->>F: ReleaseReservation(booking_id)
        F->>FD: transaction: tombstone + return seats + RELEASED + cache revision
        B->>BD: attempt COMPENSATED
        B-->>C: error, no booking
    end
```

При недоступности Flight компенсация остаётся в БД и повторяется recovery worker.
При падении Booking до сохранения COMPENSATING worker находит старый PROCESSING.
Блокировка intent исключает конкуренцию worker с активным созданием. При
неопределённом результате COMMIT проверяется SUCCEEDED, поэтому успешная покупка
не компенсируется.

## Отмена

```mermaid
sequenceDiagram
    participant C as Client
    participant B as Booking
    participant D as Booking PostgreSQL
    participant F as Flight
    C->>B: POST /bookings/id/cancel
    B->>D: lock booking, COMMIT cancellation job
    B->>D: BEGIN, lock booking
    B->>F: ReleaseReservation(id)
    F-->>B: RELEASED (idempotent)
    B->>D: UPDATE CANCELLED + DELETE job, COMMIT
    B-->>C: CANCELLED
    Note over B,D: Recovery repeats unfinished jobs after crashes
```

## Пределы модели

Синхронной распределённой транзакции нет. Во время отказа возможна временная
резервация без подтверждённого Booking; восстановление возвращает эти места.
Гарантия итоговой согласованности требует сохранности PostgreSQL и последующего
восстановления доступности сервисов. Резервации подтверждённых бронирований не
истекают автоматически; EXPIRED предусмотрен enum контракта, отдельная политика
временных удержаний заданием не требуется.

После смены статуса рейса существующие бронирования сохраняются. UpdateFlight
не запускает массовые возвраты: эта бизнес-операция не входит в заданный API.
