# Независимая проверка Backend API

## Итог

Дата проверки: 14 ноября 2026 года.

Статус: **PASS**.

Backend независимо проверен отдельным агентом как черный ящик. После исправления и
повторной проверки открытых дефектов уровня P0/P1 нет. Production-код тестирующий
агент не изменял.

## Проверенный результат кейса 19

| Показатель | Результат |
|---|---|
| Загружено файлов | 26 |
| Классифицировано операций | 19 |
| Заключение | `REPORT_UNRELIABLE` |
| Дельта | 12 500,00 руб. |
| Статьи `0200`, `0300`, `0900` | `CORRECT` |
| Статья `0800` | `MISMATCH` |
| Evidence расхождения | 5 из 5 доступны |
| Координаты ячейки КФЗ | Нормализованы |
| Документ после EPHEMERAL-обработки | `410 Gone` |

## Матрица проверки

| Группа | Статус | Фактический результат |
|---|---|---|
| Docker Compose и regression suite | PASS | 38 из 38 тестов |
| Health, readiness, capabilities | PASS | Режим и лимиты отражаются корректно |
| OpenAPI | PASS | Заявленные endpoints присутствуют |
| Полный EPHEMERAL case19 | PASS | Ожидаемая дельта и заключение |
| Evidence и SHA-256 | PASS | 5 из 5 ссылок, bbox нормализован |
| Обычная очистка EPHEMERAL | PASS | Workspace удален после результата |
| Ошибка pipeline | PASS | Контролируемый `FAILED`, workspace удален |
| Restart во время OCR | PASS | Осиротевший workspace удален при старте |
| Timeout pipeline-стадии | PASS | Публикуется в capabilities, ошибка контролируема |
| Параллельные задания | PASS | Разные UUID и изолированные workspace |
| Path traversal | PASS | `422 UNSAFE_FILE_NAME` |
| Неподдерживаемый формат | PASS | `415 UNSUPPORTED_DOCUMENT` |
| Повтор имени | PASS | `422 DUPLICATE_FILE_NAME` |
| Отсутствующий отчет | PASS | `422 REPORT_FILE_MISSING` |
| Невалидный context | PASS | `422 INVALID_CONTEXT` |
| EPHEMERAL rerun | PASS | `409 SOURCE_DOCUMENTS_NOT_AVAILABLE` |
| PERSISTENT case19 | PASS | Финансовый результат совпадает с EPHEMERAL |
| PERSISTENT restart/history | PASS | Результат восстановлен |
| PERSISTENT rerun | PASS | Повторный результат идентичен |
| PERSISTENT delete | PASS | `204`, затем `404`, файлы удалены |
| PERSISTENT retention | PASS | Просроченный запуск удален при старте |
| Утечки в логах | PASS | Имена и содержимое документов не обнаружены |
| Финансовые edge cases | PASS | Missing, REVIEW, OCR-ошибки и итоги покрыты |

## Найденный и исправленный дефект

Первичная независимая проверка выявила P1: при рестарте контейнера во время OCR
EPHEMERAL-сессия исчезала из памяти, но ее временные файлы оставались в writable layer
контейнера.

Исправление:

- при старте EPHEMERAL backend удаляет осиротевшие workspace из `API_TEMP_ROOT`;
- добавлен regression-тест `upload → restart during processing → temporary root empty`;
- каждая pipeline-стадия получила настраиваемый timeout, после которого job завершается
  контролируемой ошибкой и проходит обычную очистку.

Повторная независимая проверка подтвердила, что полный комплект существовал в активном
workspace до рестарта, а после readiness нового процесса временный корень был пуст.

## Остающиеся production-ограничения

Эти ограничения не блокируют хакатонный backend, но обязательны перед публичным
production-развертыванием:

- авторизация и разграничение доступа;
- проверка MIME по сигнатуре, а не только расширения;
- шифрование persistent storage и аудит доступа;
- отдельный общий job store и очередь для нескольких API workers;
- фоновый janitor, если retention должен срабатывать без старта и входящих запросов.

