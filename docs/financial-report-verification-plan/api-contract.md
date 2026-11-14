# API-контракт финансовой сверки

**Версия:** `v1` (`api_version: 1.0`).  
**Машиночитаемый источник схем:** `/openapi.json`; Swagger UI — `/docs`.

Этот файл — краткий контракт для frontend, интеграций и тестов. Он описывает
реализованный API, а не будущие проекты, версии документов и историю аудита.

## Базовые правила

- Base URL локального запуска: `http://127.0.0.1:8000`.
- Поддерживаемые файлы: `.pdf`, `.png`, `.docx`, `.xlsx`, `.txt`.
- Обработка локальная: `external_services: false`.
- Первый `POST /api/v1/verifications` создает cookie
  `financial_verification_session` (`HttpOnly`, `SameSite=Lax`). Браузер
  передает ее автоматически; frontend не должен читать или собирать ее сам.
- Проверки, документы, страницы и evidence доступны только сессии-владельцу.
  Чужая или отсутствующая сессия получает `404 VERIFICATION_NOT_FOUND`, без
  раскрытия существования объекта.
- В `EPHEMERAL`-режиме workspace существует только до TTL. В `PERSISTENT`
  хранение регулируется retention policy. Режим всегда читается из ответа.

## Сквозной сценарий

```text
POST /verifications  →  202 + verification_id + session cookie
GET  /{id}/events    →  SSE-прогресс (или polling GET /{id})
GET  /{id}/result    →  VerificationResult
GET  /{id}/evidence  →  доказательства и preview
GET  /{id}/export    →  JSON либо Markdown
```

## Сервисные методы

| Метод и путь | Успех | Назначение |
|---|---:|---|
| `GET /health/live` | 200 | Процесс запущен: `{"status":"ok"}`. |
| `GET /health/ready` | 200 / 503 | Конфигурация доступна: `{"status":"ready","storage_mode":"EPHEMERAL"}`. |
| `GET /api/v1/capabilities` | 200 | Форматы, лимиты, экспорты и storage-режим. |

`capabilities.storage` содержит `mode`, `documents_persisted`,
`results_persisted`, `result_ttl_seconds`, `retention_days`,
`history_available`, `rerun_without_upload`.

## Создание проверки

`POST /api/v1/verifications` принимает `multipart/form-data`.

| Поле | Тип | Обязательно | Описание |
|---|---|---:|---|
| `context` | JSON-строка | да | Контекст; неизвестные поля игнорируются. |
| `files` | массив файлов | да | КФЗ и связанные документы с уникальными именами. |
| `article_rules` | JSON-строка | нет | Массив временных статей: `code`, `name`, `signals`. Признаки интерпретируются как обычный текст, не regex. |

Минимальный `context`:

```json
{
  "report_file": "01_Карточка_учета_фактических_затрат_этап_3.pdf",
  "target_row_label": "3 этап, 2026 г.",
  "contract_number": "24/03/15-01",
  "period_from": "2026-01-01",
  "period_to": "2026-05-31",
  "case_id": "case-19"
}
```

`report_file`, `contract_number`, `period_from`, `period_to` обязательны.
`report_file` должен входить в `files`; дата окончания не раньше даты начала.

Ответ `202`:

```json
{
  "verification_id": "9a8a…",
  "status": "QUEUED",
  "storage": {
    "mode": "EPHEMERAL",
    "documents_persisted": false,
    "results_persisted": false,
    "result_available_until": "2026-11-14T20:00:00Z"
  }
}
```

Ошибки: `422 EMPTY_DOCUMENT_SET`, `422 INVALID_CONTEXT`,
`422 DUPLICATE_FILE_NAME`, `422 REPORT_FILE_MISSING`, `413 TOO_MANY_FILES`,
`413 FILE_TOO_LARGE`, `413 DOCUMENT_SET_TOO_LARGE`, `415 UNSUPPORTED_DOCUMENT`.

## Проверки и прогресс

| Метод и путь | Успех | Назначение |
|---|---:|---|
| `GET /api/v1/verifications` | 200 | `{"items":[VerificationStatusResponse]}` только текущей сессии. |
| `GET /api/v1/verifications/{id}` | 200 | Статус одной проверки. |
| `GET /api/v1/verifications/{id}/events` | 200 SSE | События `progress`, `completed`, `failed`. |
| `POST /api/v1/verifications/{id}/rerun` | 202 | Повторная проверка, пока исходный workspace не истёк. |
| `POST /api/v1/verifications/{id}/possible-duplicate-decisions` | 202 | Решение `INCLUDE`/`EXCLUDE` по вероятному дублю и честный повторный запуск. |
| `DELETE /api/v1/verifications/{id}` | 204 | Удаление; активная проверка — `409 VERIFICATION_RUNNING`. |

Статусы: `QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED`, `DELETED`.

```json
{
  "verification_id": "9a8a…",
  "status": "PROCESSING",
  "stage": "RECONCILIATION",
  "progress": 70,
  "message": "Сверяем статьи КФЗ",
  "created_at": "2026-11-14T19:01:00Z",
  "updated_at": "2026-11-14T19:01:24Z",
  "error": null,
  "storage": {"mode":"EPHEMERAL", "documents_persisted":false, "results_persisted":false}
}
```

SSE имеет media type `text/event-stream`; `data` содержит status payload.
Клиенту рекомендуется fallback polling через `GET /api/v1/verifications/{id}`.

## Результат и экспорт

| Метод и путь | Успех | Примечание |
|---|---:|---|
| `GET /api/v1/verifications/{id}/result` | 200 JSON | До завершения — `409 RESULT_NOT_READY`; при падении — `409 VERIFICATION_FAILED`. |
| `GET /api/v1/verifications/{id}/export?format=json` | 200 JSON | Тот же `VerificationResult`. |
| `GET /api/v1/verifications/{id}/export?format=markdown` | 200 `text/markdown` | Markdown-отчет с `Content-Disposition`. |

Result содержит минимум `summary`, `articles`, `documents`, `evidence`,
`annotations`, `findings`. Денежные значения передаются Decimal-строками,
например `"12500.00"`; клиент не должен складывать их через binary float.

Статья: `article_code`, `article_name`, `report_value`, `supported_value`,
`delta_report_minus_supported`, `status`, `reason_code`, `formula`,
`report_evidence_id`, `source_evidence_ids`. `MISMATCH` — финансовое
расхождение; `INSUFFICIENT_DATA` и `UNSUPPORTED_ARTICLE` — границы проверки,
а не финансовая ошибка.

## Документы и evidence

| Метод и путь | Успех | Границы контракта |
|---|---:|---|
| `GET /api/v1/verifications/{id}/documents` | 200 JSON | `{"items":[Document]}`. |
| `GET /…/{id}/documents/{document_id}/content` | 200 bytes | Только PERSISTENT; в EPHEMERAL — `410 DOCUMENT_CONTENT_EXPIRED`. |
| `GET /…/{id}/documents/{document_id}/pages/{page}` | 200 image | Preview страницы, не публичная ссылка на оригинал. |
| `GET /…/{id}/documents/{document_id}/spreadsheet?sheet={name}` | 200 JSON | Ограниченный grid только для `.xlsx`. |
| `GET /…/{id}/documents/{document_id}/text-preview` | 200 JSON | Структурированный preview только для `.docx` и `.txt`. |
| `GET /…/{id}/evidence/{evidence_id}` | 200 JSON | Один нормализованный источник доказательства. |

`EvidenceReference` содержит `evidence_id`, `document_id`, `raw_text`,
`confidence`, `locator`; для page evidence — готовый относительный `page_url`.
Он не публичен: при каждом запросе проверяется cookie-сессия. API никогда не
отдает server filesystem path.

### XLSX preview

```json
{
  "sheet": "Ведомость",
  "sheets": ["Ведомость"],
  "range": "A1:D8",
  "truncated": false,
  "columns": ["A", "B", "C", "D"],
  "row_numbers": [1, 2, 3, 4, 5, 6, 7, 8],
  "rows": [[
    {"coordinate": "C8", "value": "1110000", "formula": "=SUM(C6:C7)"}
  ]]
}
```

- `value` — сохраненный результат Excel; если кэша нет, backend вычисляет
  только ограниченный простой `SUM`. Неизвестная формула не заменяется догадкой.
- `formula` — исходная формула либо `null`.
- Grid ограничен 120 строками и 40 колонками; `truncated: true` означает, что
  клиент видит не всю книгу.
- UI показывает `value`; по двойному клику открывает `formula` и подсвечивает
  ссылки текущего листа.

Ошибки: `404 SPREADSHEET_NOT_FOUND`, `404 SHEET_NOT_FOUND`,
`410 SPREADSHEET_EXPIRED`, `422 SPREADSHEET_PREVIEW_FAILED`,
`429 SPREADSHEET_PREVIEW_RATE_LIMITED`. Preview принимает не более 20 запросов
на одну проверку в минуту.

## Stateless tools

Эти методы не создают verification и не дают доступ к ранее загруженным файлам.

| Метод | Тело | Результат |
|---|---|---|
| `POST /api/v1/tools/extract` | `multipart/form-data`, `file` | Извлеченный текст и метаданные одного документа. |
| `POST /api/v1/tools/annotate` | JSON `{fields, confidence_threshold?}` | `{"annotations":[…]}`. |
| `POST /api/v1/tools/reconcile` | JSON `{operations, report_fields, context, rules?}` | Результат сверки. |

`fields[]` в annotate: `field_id`, `text`, `document_id?`, `field_type`
(`money`, `contract`, `date`, `text`), `expected_value?`, `confidence?`.
`confidence_threshold` по умолчанию `0.80`.

## Общие ошибки и совместимость

Доменные ошибки используют `detail.code`:

```json
{"detail":{"code":"RESULT_NOT_READY","message":"…"}}
```

| HTTP | Действие клиента |
|---:|---|
| 404 | Не раскрывать существование объекта; показать «не найдено или нет доступа». |
| 409 | Показать текущий статус, повторить позже либо предложить перезагрузить комплект. |
| 410 | Workspace удален по TTL — предложить загрузить комплект заново. |
| 413 | Показать превышенный лимит. |
| 415 | Показать неподдерживаемый формат. |
| 429 | Снизить частоту запросов preview и повторить позже. |
| 422 | Показать `detail.message`, если оно есть. |
| 503 | Конфигурация API не готова. |

В пределах `v1` новые необязательные поля могут появляться в ответах; клиент
должен игнорировать неизвестные поля. Удаление либо смена смысла обязательного
поля требует новой версии API.
