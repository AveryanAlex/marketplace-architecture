# Защита: порядок демонстрации

## 0–3 минуты: запуск

```bash
docker compose up --build
```

Показать выполнение генерации при сборке, логи миграций и запуск API.
Если слои сборки закешированы, показать Dockerfile и `scripts/generate.sh`.
Открыть http://localhost:8080/docs: исходный контракт, отдельные DTO,
ограничения полей, ответы ошибок, BearerAuth и описания ролей.

## 3–9 минут: E2E и SELECT

Во втором терминале:

```bash
make e2e
```

Сценарий делает настоящие HTTP-запросы и показывает SQL с проверенными результатами:

1. Товар 100 × 3, скидка 10%: stock=7, total=270, discount=30, promo_uses=1.
2. Изменение цены товара на 120 не меняет snapshot=100 в заказе.
3. Обновление количества до 1: stock=9, total=120, скидка снимается из-за min=200,
   promo_code_id=null, promo_uses=0.
4. PAYMENT_PENDING → CANCELED возвращает stock=10. Повторная отмена запрещена.
5. Другой заказ проходит все состояния до COMPLETED; отмена после оплаты запрещена.
6. Товар архивируется, строка остаётся доступной через GET и SQL.

Пароли и токены сценарий не выводит. Аккаунты каждого запуска уникальны.

## 9–13 минут: альтернативные запросы

Использовать Swagger UI. Зарегистрировать USER и SELLER, выполнить login,
скопировать access_token в Authorize. ADMIN создаётся при старте Compose.

Проверить по выбору преподавателя:

- price=0, stock=-1, пустое name, некорректный UUID, items=[], quantity=1000 → 400.
- Несуществующий товар → PRODUCT_NOT_FOUND, INACTIVE/ARCHIVED → PRODUCT_INACTIVE.
- Несколько дефицитных товаров → INSUFFICIENT_STOCK со списком всех дефицитов.
- Промокод отсутствует, истёк, неактивен или исчерпан → PROMO_CODE_INVALID.
- Сумма ниже минимальной для промо → PROMO_CODE_MIN_AMOUNT, остаток не меняется.
- Чужой заказ → ORDER_OWNERSHIP_VIOLATION; чужой товар SELLER → ACCESS_DENIED.
- Второе создание за минуту → ORDER_LIMIT_EXCEEDED, Retry-After.
- После окончания cooldown второй активный заказ → ORDER_HAS_ACTIVE.
- PUT в PAYMENT_PENDING и отмена в PAID → INVALID_STATE_TRANSITION.
- Повторное использование refresh → REFRESH_TOKEN_INVALID.

Показать строку JSON-лога с таким же X-Request-Id, как в HTTP-ответе.
После неудачных операций выполнить SELECT и показать отсутствие частичных изменений.

## 13–17 минут: код и тесты

```bash
make test
```

Объяснить:

- `openapi/marketplace.yaml` → `scripts/generate.sh` → `codegen/templates/` → generated routes/DTO.
- `app/main.py` подключает generated routers; `app/handlers.py` вызывает бизнес-сервисы.
- `app/orders/service.py`: одна транзакция, проверка cooldown/active, порядок блокировок,
  резервирование, snapshot и возврат резервов.
- `app/promo_codes/service.py`: точная денежная арифметика и округление.
- `app/auth/security.py` и `service.py`: проверка JWT, роли, хеширование и ротация refresh.
- Alembic-миграция: enum, FK, CHECK, частичный уникальный индекс, индекс status и триггер.
- `tests/test_concurrency.py`: реальные параллельные запросы к PostgreSQL.

## 17–20 минут: теория

Темы для подготовки:

- Contract-first и code-first, required и nullable, PUT и PATCH, идемпотентность DELETE.
- ACID, READ COMMITTED, REPEATABLE READ, блокировки FOR UPDATE и порядок захвата.
- Почему async не защищает от гонок и почему блокировки asyncio недостаточны для нескольких процессов.
- Почему проверка отсутствующего заказа без блокировки пользователя допускает гонку.
- Decimal против float, NUMERIC(12,2), снимок цены и ROUND_HALF_UP.
- Подпись JWT против шифрования, access против refresh, exp/issuer/audience/role.
- Argon2 против обычного SHA-256 для паролей; почему SHA-256 подходит для случайных refresh-токенов.
- Миграции против create_all, уникальный частичный индекс и триггеры timestamps.
- AsyncSession на запрос; почему одна сессия не должна одновременно использоваться несколькими задачами.

Неоднозначные бизнес-правила и принятые трактовки перечислены в README.
