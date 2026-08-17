<!-- ВЕНДОРЕННАЯ КОПИЯ — НЕ РЕДАКТИРОВАТЬ ЗДЕСЬ -->

> **Вендоренная копия.** Источник: `SpiritTechDevelopment/SpiritVPN`,
> файл `dev/BACKEND_DOMAIN_AGREEMENTS.md`, коммит `8f69f253` (2026-08-07).
> Правки вносятся в репозитории-источнике и подтягиваются сюда целиком —
> см. [README.md](README.md). Единственное локальное отличие от источника —
> эта врезка.

# SpiritVPN backend: нормативная архитектура v1

Статус: **утверждённая целевая спецификация backend v1**.

Этот документ является единственным источником истины о доменной модели и
границах центрального backend SpiritVPN. Старые `BACKEND_TECHNICAL_SPEC.md` и
`NODE_AGENT_CONTRACT.md` описывают отменённый вариант архитектуры и не применяются
к реализации v1.

Контракт backend↔node-agent — `contracts/nodeagent/v1/node_agent.proto` из
инфраструктурного репозитория (pull-модель usage, push `EnsureUser*`/`ReconcileUsers`).
Он со-owned с командой агента и до старта имплементации обязан быть **завендорен в
backend-репо как замороженный baseline**. Единственное требуемое дополнение — поле
`egress_key` в `User` (секция 9); оно pending и требует подписи владельца
agent-стороны до реализации. Manifest-контракт (§6) аналогично со-owned с infra
CI/CD и требует отдельного согласованного proto. Этот документ описывает
backend-сторону; SQL migrations обязаны ему соответствовать, расхождения с proto
устраняются согласованно до merge или deploy.

## 1. Назначение и граница системы

Backend является центральным control plane для доступа customer к VPN-флотам.

Backend отвечает за:

- приём идемпотентных customer-команд через gRPC;
- хранение желаемого customer state в PostgreSQL;
- получение и атомарное применение infrastructure manifest;
- хранение проекции VPN-флотов, логических нод и BRIDGE → EXIT связей;
- создание индивидуальных VLESS `client_uuid` и псевдонимных accounting ID;
- применение глобального срока доступа и отдельной customer traffic quota на
  каждой ноде;
- формирование VLESS-ссылок по актуальному manifest;
- надёжную доставку команд добавления и удаления пользователей node-agent;
- предоставление агенту полного desired snapshot его ноды для восстановления;
- получение traffic-батчей от node-agent (pull) и начисление quota;
- аудит security-sensitive и destructive действий;
- health, readiness и metrics.

Backend не отвечает за:

- реализацию и локальное состояние node-agent;
- непосредственную конфигурацию Xray;
- создание Xray inbound, outbound и routing rules;
- выбор транспорта между BRIDGE и EXIT;
- создание и ротацию инфраструктурных credentials BRIDGE → EXIT;
- DNS, перенос доменов и жизненный цикл физических серверов;
- сбор исходных Xray counters и запись исходных traffic events;
- отдельный operator API для ручного управления runtime state;
- billing, payment и принятие решения о праве customer на подписку.

Backend никогда не обращается к Xray напрямую. Все изменения runtime users
выполняет node-agent через loopback Xray API.

## 2. Архитектура приложения

Backend реализуется как модульный монолит с двумя runtime-ролями из одной кодовой
базы:

```text
External product service ──gRPC──> API ─┐
Infrastructure pipeline ───gRPC──> API ─┼──> PostgreSQL
Workers ────────────────────────────────┘        ^
                                                  | (обновление по результату)
Workers ──gRPC + mTLS──> node-agents:
    push  EnsureUserPresent/Absent, ReconcileUsers
    pull  GetNodeState (usage + activity)
node-agent ──loopback──> Xray API (Handler / Stats / Routing)
```

- `api` принимает запросы и фиксирует durable state;
- `worker` материализует manifest changes, доставляет agent operations, выполняет
  retry и тянет usage/activity от агентов через `GetNodeState`;
- роли могут запускаться в одном процессе для development и раздельно в
  production;
- несколько экземпляров API и workers допускаются;
- координация workers выполняется только через PostgreSQL locks/leases.

Слои приложения:

```text
Inbound adapters → Application use cases → Domain → Ports → Adapters
```

Transport handlers выполняют аутентификацию, wire-валидацию и mapping. Они не
содержат бизнес-логику. Domain не зависит от PostgreSQL, gRPC, Xray или wire-схемы
manifest.

Основные application use cases:

```text
ApplyCustomerAccess
GetCustomerAccessLinks
ApplyFleetManifest
MaterializeManifestRevision
DispatchAgentOperation
ReconcileNodeUsers
PullNodeUsage
RetryFailedOperation
```

## 3. Authority и идентичность

| Данные | Authority | Durable storage |
|---|---|---|
| Customer access и его fleet | Backend | PostgreSQL |
| `client_uuid`, accounting ID и desired node users | Backend | PostgreSQL |
| Agent operations и apply state | Backend | PostgreSQL |
| Fleet, ноды, связи и публичные параметры | Infrastructure manifest | PostgreSQL projection |
| Xray inbound/outbound/routing | Infrastructure | Node configuration |
| BRIDGE → EXIT credentials | Infrastructure | Вне backend |
| Runtime users | Node-agent/Xray | Не является central authority |
| Traffic delta batches | Node-agent (local spool) | Pull в PostgreSQL по cursor |
| Traffic totals | Backend | PostgreSQL |

`customer_id` — непрозрачная строка длиной 1–256 байт. Backend не преобразует её
в число, не извлекает семантику и не помещает в Xray email, логи или metrics.

Customer приходят из одной внешней системы. В v1 ключом является `customer_id`, а
не `(source, customer_id)`.

`node_id` и `routing_key` — стабильные инфраструктурные идентификаторы. IP-адрес,
домен, agent endpoint и физический сервер идентичностью ноды не являются.
`routing_key` однозначно идентифицирует одну направленную BRIDGE-связь внутри
fleet; отдельного `bridge_id` или `bridge_relation_id` нет.

## 4. Доменная модель customer access

Один `customer_id` имеет не более одного `vpn_fleet_id`. Смена fleet существующего
customer в v1 не поддерживается.

`vpn_fleet_id` — постоянная доменная идентичность. После первого успешного
manifest, содержащего fleet, его ID не удаляется и не переиспользуется.
Fleet может временно не содержать нод и relations, но сам fleet остаётся в
каждом последующем полном manifest snapshot.

Для действующего customer entitlement backend создаёт:

- один индивидуальный `FREEDOM` access для каждой ноды fleet;
- один индивидуальный `BRIDGE` access для каждой bridge relation fleet.

На одной ноде у customer всегда не более одного `FREEDOM` access, но может быть
несколько `BRIDGE` access: по одному для каждой relation, у которой эта нода
является `entry_node_id`. Ограничения «не более одного BRIDGE на customer/node»
нет.

```text
link_count = fleet_node_count + bridge_relation_count
```

Например, fleet с нодами `NL-1`, `DE-1` и связью `NL-1 → DE-1` даёт customer три
access:

```text
NL-1 → Freedom
DE-1 → Freedom
NL-1 → DE-1
```

У каждого access собственные:

- `access_id`;
- `client_uuid`;
- `accounting_id`;
- входная нода, на которую устанавливается Xray user;
- desired/apply state.

Для `FREEDOM` входной нодой является сама нода. Для `BRIDGE` входной нодой
является `entry_node_id` соответствующего route; customer `client_uuid` не
устанавливается на EXIT.

Отдельной команды ручного enable/disable в v1 нет. Доступ прекращается только при
expiry или достижении node quota. Customer, его entitlement, credentials и
история физически не удаляются. Повторный `ApplyCustomerAccess` с более поздним
expiry выполняет renewal и повторно использует прежние `client_uuid` и accounting
IDs.

Логическая цель access — `logical_target_key`: для FREEDOM это `node_id` его ноды,
для BRIDGE — `routing_key` relation. Он стабилен для «одной и той же» цели поверх
поколений и используется в уникальности access и сортировке ответов.

Удалённый из manifest access становится `RETIRED`, больше не выдаётся и не
восстанавливается. Если та же `logical_target_key` позднее добавлена снова,
создаётся новое поколение с `generation = max(generation) + 1` для данной
`(customer_id, kind, logical_target_key)` (первое поколение — `1`), новым
`client_uuid` и accounting ID.

Эффективный access на конкретной входной ноде существует только когда
одновременно:

```text
current_time < expires_at
AND node_quota_period_usage_bytes < usage_quota_bytes
```

Срок действия применяется ко всему customer. Quota применяется независимо на
каждой ноде fleet. На одной ноде трафик всех FREEDOM и BRIDGE access данного
customer суммируется в один node quota period. Превышение на одной ноде не влияет
на access того же customer на других нодах.

## 5. Customer gRPC API

Нормативный внешний сервис:

```protobuf
service CustomerAccessService {
  rpc ApplyCustomerAccess(ApplyCustomerAccessRequest)
      returns (ApplyCustomerAccessResponse);
  rpc GetCustomerAccessLinks(GetCustomerAccessLinksRequest)
      returns (GetCustomerAccessLinksResponse);
}

message ApplyCustomerAccessRequest {
  string customer_id = 1;
  int64 vpn_fleet_id = 2;
  uint64 usage_quota_bytes = 3;
  int64 expires_at_epoch_sec = 4;
  uint64 command_number = 5;
}

message ApplyCustomerAccessResponse {}

message GetCustomerAccessLinksRequest {
  string customer_id = 1;
}

enum AccessKind {
  ACCESS_KIND_UNSPECIFIED = 0;
  ACCESS_KIND_FREEDOM = 1;
  ACCESS_KIND_BRIDGE = 2;
}

enum AccessLinkState {
  ACCESS_LINK_STATE_UNSPECIFIED = 0;
  ACCESS_LINK_STATE_PENDING = 1;
  ACCESS_LINK_STATE_READY = 2;
  ACCESS_LINK_STATE_BLOCKED = 3;
  ACCESS_LINK_STATE_FAILED = 4;
}

enum AccessBlockReason {
  ACCESS_BLOCK_REASON_UNSPECIFIED = 0;
  ACCESS_BLOCK_REASON_TIME_EXPIRED = 1;
  ACCESS_BLOCK_REASON_TRAFFIC_QUOTA_EXHAUSTED = 2;
}

message CustomerAccessLink {
  AccessKind kind = 1;
  AccessLinkState state = 2;
  optional AccessBlockReason block_reason = 3;
  optional string uri = 4;
}

message GetCustomerAccessLinksResponse {
  repeated CustomerAccessLink links = 1;
}
```

Все поля request обязательны; `vpn_fleet_id > 0`, `usage_quota_bytes > 0`,
`command_number > 0`.

`command_number` — монотонно возрастающий по customer счётчик команд
product-сервиса. Он задаёт порядок команд и защищает от переупорядочивания и
повторов в сети; см. правила обработки ниже.

`expires_at_epoch_sec` — абсолютный UTC Unix timestamp в секундах, а не duration.
Имя `link_validity_period` не используется, поскольку значение содержит момент
окончания, а не длительность. При создании customer и при renewal timestamp должен
находиться в будущем.

`usage_quota_bytes` задаёт одинаковый лимит отдельно для каждой ноды и хранится в
байтах. Если product API продаёт гигабайты, он
явно преобразует их в байты по своей продуктовой единице до вызова backend. Это
устраняет неоднозначность между GB (`10^9`) и GiB (`2^30`).

`quota_period_id` — backend-generated unique (UUID), внутренний идентификатор,
во внешний контракт не передаётся. Первый успешный `ApplyCustomerAccess` создаёт
начальный период.
Повторный Apply с тем же expiry не сбрасывает уже учтённый traffic. Apply с более
поздним expiry является renewal и атомарно:

- закрывает текущий quota period в момент commit;
- создаёт новый внутренний quota period;
- устанавливает одинаковый новый `usage_quota_bytes` и нулевой total на каждой
  ноде fleet;
- создаёт для нового периода node usage с нулевыми counters и без `exhausted_at`;
- устанавливает новый будущий `expires_at`;
- материализует отсутствующие access для текущей topology fleet;
- создаёт необходимые `EnsureUserPresent` для всех актуальных access.

Существующие access сохраняют `client_uuid` и accounting IDs; только новая logical
target, появившаяся в manifest пока customer был expired, получает новые
`client_uuid` и accounting ID.

Renewal начинает действовать в момент commit Apply; запланированное заранее
продление в v1 не поддерживается.

Правила обработки:

1. Product service остаётся единственным writer и присваивает `command_number` в
   порядке выпуска команд. Корректность больше не зависит от порядка доставки:
   backend упорядочивает команды сам по этому номеру.
2. Backend хранит `last_command_number` в `customer_entitlements` и под row lock
   этой строки сравнивает его с входящим. Команда с
   `command_number <= last_command_number` — устаревший или повторный вызов:
   backend не применяет никаких side effects и возвращает `OK`. Так поглощаются
   переупорядоченные и повторно доставленные команды.
3. Команда с `command_number > last_command_number` принимается к обработке;
   `last_command_number` обновляется только при её успешном commit (принятие или
   валидный no-op). Отклонённые команды (`NOT_FOUND`, `FAILED_PRECONDITION`)
   номер не двигают.
4. Принятый Apply, не меняющий уже сохранённое целевое состояние, не создаёт новых
   agent operations, но всё равно обновляет `last_command_number`.
5. Другой fleet для существующего customer возвращает `FAILED_PRECONDITION`.
6. Неизвестный fleet возвращает `NOT_FOUND`.
7. Для существующего customer Apply с тем же expiry может изменить quota без
   сброса totals. Если quota также совпадает, запрос является no-op.
8. Apply с expiry строго больше сохранённого выполняет renewal и один раз
   сбрасывает traffic независимо от изменения quota.
9. Apply с expiry меньше сохранённого возвращает `FAILED_PRECONDITION`: сокращение
   срока в v1 не поддерживается. Устаревшая команда с меньшим expiry уже отсекается
   правилом 2 по `command_number` и сюда не доходит.

В транзакции принятия новой entitlement-команды backend пересчитывает производные
expiry и node quota states. Понижение quota устанавливает `exhausted_at` каждой
ноде, где total достиг нового лимита. Повышение quota очищает `exhausted_at` только
у нод, где total снова ниже лимита. Renewal начинает новый quota period с нулевым
total и пустым `exhausted_at` на каждой ноде. `EnsureUserPresent` создаётся для
access тех нод, где entitlement не истёк и quota текущего периода не исчерпана.

Успешный `ApplyCustomerAccess` означает, что desired state, access changes и
durable agent operations зафиксированы в PostgreSQL. Пустой response не обещает,
что агенты уже применили операции.

`GetCustomerAccessLinks` возвращает все ожидаемые access с состоянием:

```text
PENDING | READY | BLOCKED | FAILED
```

Неизвестный `customer_id` возвращает `NOT_FOUND`.

Поле `uri` присутствует только для `READY`. Частично готовый fleet возвращает
готовые URI и состояния остальных access; недоступность одной ноды не скрывает
работающие ссылки других нод. `RETIRED` historical access во внешний API не
возвращаются.

При `BLOCKED` поле `block_reason` обязательно. Если одновременно применимы expiry
и quota, наружу возвращается `TIME_EXPIRED`. Для остальных states `block_reason`
отсутствует. Отдельный `display_name` не возвращается: человекочитаемое имя из
manifest уже входит в fragment готовой VLESS URI.

Все текущие links одного customer возвращаются одним ответом без pagination.
Список сортируется по внутреннему `(kind, logical_target_key, access_id)`. Response
с URI всегда имеет эквивалент `Cache-Control: no-store` на поддерживающем metadata
transport.

Отдельный effective/block state не хранится. `TIME_EXPIRED` выводится из
`current_time >= expires_at`, а `TRAFFIC_QUOTA_EXHAUSTED` — из непустого
`node_quota_usage.exhausted_at` текущего периода.

При `current_time >= expires_at` backend глобально блокирует customer, переводит
все его access в `ABSENT` и создаёт `EnsureUserAbsent` для всех входных нод. При
достижении quota backend переводит в `ABSENT` только access customer с
соответствующим `entry_node_id` и создаёт Remove operations только для этого Xray.
URI заблокированных access перестают выдаваться после commit. Исторические записи
и encrypted `client_uuid` не удаляются физически.

Только успешный renewal-переход Apply снимает `TIME_EXPIRED`. Новый quota period
или повышение лимита может независимо разблокировать отдельные ноды.

## 6. Infrastructure manifest

Manifest — полный versioned snapshot. Нормативная логическая схема:

```yaml
schema_version: 1
revision: 42
allow_destructive: false

nodes:
  - node_id: NL-1
    agent:
      endpoint: 10.0.0.11:9443
      tls_server_name: nl-1.agent.internal
      certificate_identity: spiffe://spiritvpn/node/NL-1
    public:
      address: nl.example.com
      port: 443
      reality_public_key: opaque
      server_name: www.example.org
      short_id: opaque
      fingerprint: chrome
      flow: xtls-rprx-vision
      transport: tcp
    display_name: Netherlands

fleets:
  - vpn_fleet_id: 10
    node_ids: [NL-1, DE-1]
    bridges:
      - routing_key: nl-1.to-de-1
        entry_node_id: NL-1
        exit_node_id: DE-1
        egress_tag: de-exit
        display_name: Netherlands via Germany
```

`generated_at`, `complete`, `change_id` и fleet `name` в manifest отсутствуют.
Инфраструктурные route credentials и `client_uuid` в manifest запрещены.

`egress_tag` — тег exit-outbound на входной ноде для этой relation; неймингом
владеет инфраструктура (та же строка, что у outbound в конфиге Xray). `egress_tag`
обязателен для каждого bridge. Backend хранит его в access и передаёт агенту
дословно как `User.egress_key` (§7, §9), ничего не деривя; для FREEDOM `egress_key`
— зарезервированная локаль (`""` = `direct`).

Source YAML хранится в инфраструктурном репозитории. CI/CD валидирует его,
преобразует в internal gRPC request и вызывает backend с mTLS identity
`manifest-writer`. Backend не читает manifest с filesystem и не загружает его при
старте контейнера. Точная protobuf-схема создаётся на этапе реализации и обязана
однозначно представлять эту логическую структуру.

Валидация manifest:

- `schema_version`, `revision` и `allow_destructive` обязательны;
- `schema_version` для этого документа равна `1`, `revision > 0`;
- v1 отклоняет неизвестные поля;
- `revision` глобальна и строго возрастает, но не обязана быть последовательной;
- canonical digest вычисляется backend из `schema_version` и отсортированного
  desired snapshot без request-scoped `allow_destructive`;
- повтор revision с тем же canonical digest идемпотентен, с другим digest
  отклоняется; более старая revision отклоняется;
- rollback выполняется прежним desired snapshot с новой большей revision;
- `node_id` и fleet ID уникальны в snapshot, `routing_key` уникален внутри fleet;
- все node references обязаны существовать;
- все fleet ID из предыдущего принятого snapshot обязаны присутствовать;
  отсутствие хотя бы одного ранее принятого fleet отклоняет весь manifest
  независимо от `allow_destructive`;
- BRIDGE и EXIT различны и входят в тот же fleet;
- пара `(entry_node_id, exit_node_id)` уникальна внутри fleet и неизменяема для
  существующего `routing_key`; перенос route требует удаления старого и добавления
  нового `routing_key`;
- `egress_tag` обязателен и непуст для каждого bridge; backend хранит и форвардит
  его дословно и не проверяет соответствие Xray-конфигу (нейминг — зона infra);
- endpoint, TLS identity, port и все обязательные REALITY-поля валидируются до
  записи;
- `transport`, `flow` и `fingerprint` обязательны; v1 поддерживает только
  `transport=tcp` и `flow=xtls-rprx-vision`, другие значения отклоняются;
- `fingerprint` не фиксирован на `chrome`: это ASCII token длиной 1–64 символа из
  `[A-Za-z0-9._-]`, который backend без семантического преобразования помещает в
  параметр `fp` VLESS URI;
- `security=reality` и `encryption=none` фиксированы протоколом v1 и в manifest не
  передаются;
- весь snapshot применяется атомарно или не применяется вообще.

Удаление ноды, membership ноды во fleet или relation обозначается отсутствием
в полном snapshot. Такое изменение требует
`allow_destructive=true`; без него manifest отклоняется. Флаг действует только на
текущий Apply и не сохраняется как разрешение для следующих revisions. Добавление
объектов и изменение endpoint/public-параметров destructive-флага не требует.
Удаление ранее принятого fleet запрещено независимо от этого флага.

Глобальное отсутствие ноды в новом manifest является утверждением infrastructure
authority, что её runtime больше не используется. Backend не выполняет drain, не
проверяет выключение сервера, не ждёт очистки Xray и не доставляет команды на
endpoint, удалённый из authority manifest. Он прекращает выдавать ссылки,
переводит установленные на удалённой ноде access в `RETIRED/ABSENT`, supersede-ит
их pending operations и больше не включает ноду в runtime health/operation
alerts. Очистка или уничтожение прежнего Xray runtime, отзыв сертификата и
PostgreSQL identity являются обязанностью infrastructure/CI/CD.

Это правило относится только к access, физически установленным на удалённой
ноде. Удаление relation или fleet membership при сохранении соответствующей entry
node создаёт обычные `EnsureUserAbsent` на всё ещё актуальный endpoint. Например,
если удалённая нода была BRIDGE exit, customer user удаляется с оставшейся BRIDGE
entry node.

После atomically committed projection backend создаёт durable
`manifest_materialization_job`. Массовое создание/удаление customer access
выполняется workers пакетами, а не внутри RPC-транзакции manifest.

Изменение endpoint, домена или REALITY-параметров при сохранении `node_id`:

- не создаёт новую ноду или access;
- не меняет `client_uuid` и accounting ID;
- обновляет новые ответы `GetCustomerAccessLinks`;
- направляет следующие agent calls на новый endpoint.

Изменение `egress_tag` существующей relation (при неизменной паре
`(entry_node_id, exit_node_id)` и `routing_key`) не destructive и трактуется как
repoint: backend обновляет `egress_key` затронутых access и переиздаёт их routing
rule (агент делает `RemoveRule` старого + `AddRule` нового) без смены `client_uuid`,
accounting ID и без нового поколения. `routing_key` при этом инвариантен.

## 7. Credential, accounting и routing

```text
client_uuid   — секретный VLESS UUID клиентского access для аутентификации;
accounting_id — стабильный псевдоним для Xray email и usage (уникален на access).
```

`client_uuid` генерируется CSPRNG как RFC 4122 UUIDv4. Accounting ID генерируется
отдельно и не содержит customer ID, username, email, телефона или других
пользовательских данных.

Нормативный формат `accounting_id`:

```text
u.<opaque-id>
```

Префикс `u.` — backend namespace: агент по нему отличает backend-owned users
(`backend_managed`) от инфраструктурных (`svc-*`). `accounting_id` уникален
глобально, не переиспользуется и не содержит customer identity. `opaque-id` —
CSPRNG-строка `[a-z0-9]` фиксированной длины (по умолчанию 20 символов base32),
поэтому `accounting_id` короткий и однозначно валиден как Xray email. Выход access
передаётся отдельным полем `egress_key` (секция 9), не встраивается в
`accounting_id`.

Выход выбирается per-user: агент по `egress_key` создаёт Xray routing rule
`user:[accounting_id] → outbound` через `RoutingService.AddRule`. Для FREEDOM
`egress_key` — локальный выход (`direct`) самой ноды; для BRIDGE — `egress_tag`
relation из manifest (дословно). Соответствие `egress_tag → outbound`
инфраструктура создаёт статически заранее; персональное правило создаёт агент на
лету.

Backend не строит Xray-конфиг. Управляемый user на входной ноде:

```json
{
  "id": "<client_uuid>",
  "email": "<accounting_id>",
  "flow": "xtls-rprx-vision"
}
```

BRIDGE → EXIT использует один инфраструктурный route credential (`svc-*`).
Индивидуальный customer credential действует только на клиентском inbound входной
ноды; на EXIT customer `client_uuid` не устанавливается.

`client_uuid` хранится application-level encrypted в формате
`key_id + nonce + ciphertext`
с AEAD. v1 использует один active encryption key из secret-провайдера; `key_id`
сохраняется в записи для forward-compat, но ротация ключей и decrypt-only keys в
v1 не реализуются. `client_uuid` в открытом виде никогда не попадает в логи,
metrics, traces или audit metadata.

## 8. VLESS URI

Готовая URI не хранится. Backend расшифровывает `client_uuid` только на время
ответа и строит URI из credential и текущего manifest:

```text
vless://<uuid>@<address>:<port>
  ?security=reality
  &encryption=none
  &pbk=<reality-public-key>
  &fp=<fingerprint>
  &type=<transport>
  &flow=<flow>
  &sni=<server-name>
  &sid=<short-id>
  #<url-encoded-display-name>
```

Все поля URI, кроме `<uuid>`, берутся из `manifest node.public` входной ноды
access: `address/port/pbk/fp/type/sni/sid` и `flow` (в v1 всегда
`xtls-rprx-vision`). Фрагмент `#<display-name>` — для FREEDOM из `node.display_name`,
для BRIDGE из `bridge.display_name`, url-encoded.

Accounting ID в URI не передаётся. Response с URI использует запрет кеширования и
не логируется. URI выдаётся только когда одновременно:

```text
current_timestamp < customer_entitlements.expires_at
AND node_quota_usage.exhausted_at IS NULL
AND vpn_accesses.retired_at IS NULL
AND desired_state = PRESENT
AND apply_state = APPLIED
AND target присутствует в текущем manifest
```

## 9. Desired state и agent operations

Для каждого назначения различаются:

```text
desired_state: PRESENT | ABSENT
apply_state:   PENDING | APPLIED | RETRYING | FAILED
```

`agent_operations` одновременно является журналом исполнения и transactional
outbox. Отдельная таблица outbox не используется.

Жизненный цикл операции:

```text
PENDING → IN_FLIGHT → SUCCEEDED
    |          |
    |          ├→ RETRY_WAIT → IN_FLIGHT
    |          ├→ FAILED_PERMANENT
    |          └→ SUPERSEDED (только для устаревшей desired_version
    |                         после истечения lease)
    └→ SUPERSEDED
```

Нормативный контракт агента — `NodeAgentService` из инфраструктурного
`contracts/nodeagent/v1/node_agent.proto`; backend является его клиентом. Backend
не обращается к Xray напрямую — единственный, кто трогает Xray, это агент.
Mutating-методы:

```protobuf
service NodeAgentService {
  rpc EnsureUserPresent(EnsureUserPresentRequest) returns (OperationResult);
  rpc EnsureUserAbsent(EnsureUserAbsentRequest)   returns (OperationResult);
  // GetNodeState   — pull usage/activity (секция 12)
  // ReconcileUsers — полное восстановление набора (секция 10)
  // Health
}

message User {
  string accounting_id   = 1;  // уникальный псевдонимный Xray email
  string credential_uuid = 2;  // секретный VLESS UUID; только для Present
  string flow            = 3;  // "" или "xtls-rprx-vision"
  string egress_key      = 4;  // цель выхода (см. ниже)
}

message EnsureUserPresentRequest { User user = 1; }
message EnsureUserAbsentRequest  { string accounting_id = 1; }
```

Поле `egress_key` — требование этого ТЗ к контракту агента и должно быть добавлено
в `node_agent.proto`: без него агент не может построить per-user routing rule при
fan-out. Оно идентифицирует выход access: для FREEDOM — локальный (`direct`), для
BRIDGE — `egress_tag` relation из manifest (дословно, §6). Соответствие
`egress_tag → outbound` статически создаётся инфраструктурой; персональное правило
создаёт агент на лету.

`node_id`, customer/fleet IDs и access kind не передаются. Нода определяется mTLS
identity агента; транспорт — mTLS поверх management WireGuard overlay. Deadline
обычной операции по умолчанию 5 секунд, переиспользуется один HTTP/2 channel на
ноду. `operation_id` остаётся внутренним ключом backend outbox и MAY передаваться
только как trace metadata.

Агент транслирует декларативную команду в Xray runtime API (loopback):

- `EnsureUserPresent` → `HandlerService.AddUser` (email = `accounting_id`,
  `credential_uuid`, `flow`) и, если `egress_key` не локальный,
  `RoutingService.AddRule` вида `user:[accounting_id] → outbound(egress_key)`;
- `EnsureUserAbsent` → `HandlerService.RemoveUser` по email `accounting_id` и
  `RoutingService.RemoveRule` соответствующего правила.

Это требует включённых на ноде `HandlerService`, `StatsService` и
`RoutingService`. Персональное правило `user:[accounting_id]` обязано матчиться
**раньше** дефолтного/catch-all правила ноды (`inboundTag → default_exit`/`block`),
иначе трафик уйдёт не туда; агент вставляет его с соответствующим приоритетом.
Уникальность `accounting_id` (email) обязательна: `RemoveUser` матчит по email, а
StatsService считает трафик per-email. Один `accounting_id` = один customer access.

Успешный `OperationResult` означает проверенное итоговое Xray state: для Present
существует user с точными email/uuid и (при не-локальном egress) его routing rule;
для Absent user и его rule отсутствуют, поэтому `credential_uuid` больше не
принимается. Уже точное состояние возвращает `OK` (идемпотентность).

`INVALID_ARGUMENT`, `FAILED_PRECONDITION`, `UNAUTHENTICATED`, `PERMISSION_DENIED`
и `UNIMPLEMENTED` — permanent errors. `UNAVAILABLE`, `DEADLINE_EXCEEDED` и
`ABORTED` — retryable. Другие transport/internal ошибки повторяются с backoff и
создают alert. Identity mismatch — security failure, не hot-loopится и создаёт
alert. Если `accounting_id` уже существует с другим uuid/egress, Present заменяет
user (и правило) и проверяет итог.

Правила:

- desired state, access/assignment и operation создаются в одной транзакции;
- сетевой вызов выполняется только после commit;
- operation хранит только `access_id`, тип и `desired_version`; payload (User с
  `accounting_id`, `client_uuid`, `egress_key`) собирается из актуальной строки
  access непосредственно перед RPC;
- `client_uuid` не дублируется в operation и расшифровывается только в памяти для
  `EnsureUserPresent`;
- при каждом изменении desired state operation новой `desired_version`
  создаётся в той же transaction, даже если предыдущая operation уже
  `IN_FLIGHT`;
- не отправленная устаревшая operation помечается `SUPERSEDED`;
- если предыдущая operation уже `IN_FLIGHT`, новая operation остаётся
  `PENDING` и не dispatch-ится до завершения либо истечения lease предыдущей;
- после истечения lease устаревшая operation не повторяется, а
  переводится в `SUPERSEDED`; dispatcher затем может взять operation
  актуальной desired version;
- completion старой operation не перезаписывает более новую desired version;
- одновременно на одну ноду отправляется не более одной mutating operation;
- agent unavailable не меняет состав fleet и desired state.

Agent-команды декларативны и семантически идемпотентны: повторный Ensure приводит
Xray (user + его routing rule) к указанному состоянию, повторный Absent
подтверждает отсутствие. Потеря ответа после применения безопасна — backend
повторяет ту же operation. У агента есть локальный durable snapshot backend-owned
набора (users + egress) для add-only self-heal при рестарте Xray и отдельный spool
для usage/activity (секция 12); авторитетная синхронизация и все удаления — через
`ReconcileUsers` (секция 10).

Retryable ошибки и transport timeout повторяются с exponential backoff и jitter:
начальная задержка 1 секунда, максимум 5 минут. Число retry не ограничено, пока
desired state актуален; возраст операции порождает alert. Validation, auth и
несовместимость protocol являются permanent failure и не hot-loopятся.

Permanent operation остаётся в terminal failed state. Восстановление выполняется
изменением некорректного desired state либо повторным `ReconcileUsers` после
устранения инфраструктурной причины: backend пушит агенту полный актуальный набор
и приводит Xray к нему. Отдельного operator API в v1 нет.

## 10. Восстановление ноды: локальный self-heal + authoritative reconcile

Runtime user set и runtime routing rules Xray не персистятся: после рестарта Xray
они пусты. Восстановление двухслойное:

- **Локальный add-only self-heal** (нода, быстро, без backend) — поднимает юзеров и
  их правила из локального снапшота сразу после рестарта Xray;
- **Authoritative `ReconcileUsers`** (backend) — единственный источник удалений и
  коррекции дрейфа; приводит ноду к точному desired state.

Нода определяется mTLS identity агента; `node_id` из тела запроса не принимается.

**Локальный self-heal.** Агент держит локальный durable снапшот backend-owned
набора: для каждого юзера `{accounting_id, credential_uuid, flow, egress_key}`.
Снапшот обновляется на каждый `EnsureUserPresent/Absent` и `ReconcileUsers`,
отражая последнее known desired состояние (`credential_uuid` при этом хранится на
ноде — принятый trade-off ради доступности). Рестарт Xray агент детектит по сбросу
`XrayState.uptime_seconds` (или переходу `reachable` после недоступности), а не по
нулю юзеров — пустая-но-целая нода и сброшенная различаются по uptime. При рестарте
агент переприменяет снапшот **add-only**: `AddUser` + `AddRule` на каждую запись,
восстанавливая и юзеров, и их маршруты. Он **никогда не удаляет**: устаревший
снапшот может лишь временно оставить лишнего юзера, но не убрать живого; все
удаления — за backend. Self-heal работает даже при недоступном backend и не тянет
ничего у него. На ноде, удалённой из manifest, остановка агента и self-heal —
обязанность infra teardown (§6); backend её больше не reconcile-ит.

**Authoritative reconcile.** Backend приводит ноду к точному desired state, пуша
полный набор через `ReconcileUsers`. Он инициирует это при обнаружении дрейфа —
`GetNodeState` вернул health/inventory, не совпадающие с `desired_revision`, —
периодически и после рестарта агента. Только этот слой выполняет удаления и правку
изменившихся uuid/egress.

Нормативный контракт (из `node_agent.proto`):

```protobuf
rpc ReconcileUsers(ReconcileUsersRequest) returns (ReconcileUsersResponse);

message ReconcileUsersRequest {
  uint64 desired_revision = 1;
  repeated User users = 2;   // полный набор backend-owned users с egress_key
}

message ReconcileUsersResponse {
  uint64 applied_revision = 1;
}
```

Каждая нода имеет монотонный `desired_revision`, который backend увеличивает в той
же транзакции при любом изменении набора или desired state её users. Backend в
короткой DB transaction блокирует строку ноды в share mode, фиксирует текущий
`desired_revision` и читает все её фактически разрешённые desired `PRESENT` users.
Истёкшие entitlement и исчерпавшие node quota в набор не входят, даже если expiry
worker ещё не отработал. Транзакция завершается до расшифрования и сетевого вызова.

Затем backend расшифровывает `client_uuid`, формирует `ReconcileUsersRequest` со
всеми `User` (включая `egress_key`) и зафиксированным `desired_revision` и вызывает
`ReconcileUsers`. Пустой `users` допустим и означает отсутствие backend users на
ноде. Payload собирается в память и не логируется.

Агент на время reconcile сериализует его с `EnsureUserPresent/Absent`: входящие
mutating RPC возвращают `UNAVAILABLE` и будут повторены backend. Агент валидирует
весь набор перед применением; невалидный запрос отклоняется целиком и Xray не
меняется.

При authoritative replace агент в пределах backend namespace:

- добавляет отсутствующих users (`AddUser` и, при не-локальном egress, `AddRule`);
- заменяет users с тем же `accounting_id` и другим `client_uuid`/`egress_key`;
- удаляет backend-owned users (`backend_managed`), которых нет в наборе
  (`RemoveUser` и `RemoveRule`);
- не изменяет инфраструктурные (`svc-*`) и неизвестные namespace.

Агент возвращает применённую `applied_revision`. Backend принимает результат,
только если текущий `desired_revision` ноды всё ещё равен ему. Тогда в одной
транзакции он отмечает актуальные desired states ноды как `APPLIED`, завершает
удовлетворённые agent operations и коммитит. Повтор идемпотентен. Если desired
state изменился после чтения набора, backend результат игнорирует и планирует
новый reconcile, а обычные Ensure доставляют более новое состояние.

Retryable transport errors backend повторяет с тем же `desired_revision`. Reconcile
идемпотентен: повторный вызов с тем же набором приводит к тому же состоянию.

Перед bulk removal агент делает best-effort флаш накопленного usage/activity spool;
ошибка не задерживает reconciliation. Ошибка Xray посередине применения может
оставить частично reconciled runtime, но следующий reconcile безопасно продолжает
приведение к тому же desired state.

Пустой набор означает отсутствие backend users: агент удаляет все backend-owned
users и их rules. Ошибка транспорта до применения не меняет Xray.

## 11. PostgreSQL model

Обязательные логические сущности:

```text
manifest_revisions(
  revision, digest, canonical_payload, applied_at
)

vpn_fleets(vpn_fleet_id, manifest_revision, current)
vpn_nodes(
  node_id, agent_config, public_config, desired_revision,
  manifest_revision, current
)
vpn_fleet_nodes(vpn_fleet_id, node_id, manifest_revision, current)
vpn_bridge_routes(
  vpn_fleet_id, routing_key, entry_node_id, exit_node_id,
  egress_tag, display_name, manifest_revision, current
)

customer_entitlements(
  customer_id, vpn_fleet_id, expires_at, desired_version,
  last_command_number, created_at, updated_at
)

quota_periods(
  quota_period_id, customer_id, started_at, closed_at,
  usage_quota_bytes
)

node_quota_usage(
  quota_period_id, node_id, uplink_bytes, downlink_bytes,
  total_bytes generated, exhausted_at, updated_at
)

vpn_accesses(
  access_id, customer_id, kind, logical_target_key, generation,
  entry_node_id, egress_key, accounting_id, encrypted_client_uuid,
  encryption_key_id, desired_state, apply_state, desired_version,
  created_at, retired_at
)

agent_operations(
  operation_id, node_id, access_id, operation_type, desired_version,
  status, attempt_count, next_attempt_at, lease_owner, lease_expires_at,
  last_error_code, last_error_message, created_at, completed_at
)

manifest_materialization_jobs(
  revision, cursor, status, lease_owner, lease_expires_at, updated_at
)

node_usage_cursors(
  node_id, spool_id, acked_sequence, updated_at
)

traffic_usage_items_processed(
  node_id, spool_id, sequence, accounting_id,
  access_id nullable, quota_period_id nullable, result, processed_at
)

traffic_batch_quarantine(
  node_id, spool_id, sequence, accounting_id nullable,
  reason, sanitized_payload, created_at
)

audit_events(
  audit_id, occurred_at, actor_type, actor_id, action, target_type,
  target_id, request_id, outcome, sanitized_metadata
)
```

`customer_entitlements` не хранит производный `effective_state`: истечение
определяется сравнением `expires_at` со временем PostgreSQL. Текущий quota period
не хранится отдельным указателем: это единственный период customer с
`closed_at IS NULL`. При renewal старый период закрывается и новый открывается в
один и тот же PostgreSQL timestamp, поэтому периоды образуют полуоткрытые интервалы
`[started_at, closed_at)` без пересечения.

`node_quota_usage.exhausted_at` одновременно является фактом исчерпания quota и
причиной node-level блокировки. Отдельные таблицы global/node blocks не нужны.
`encrypted_client_uuid` хранится непосредственно в `vpn_accesses`, потому что в
v1 одному access всегда принадлежит ровно один сохраняемый `client_uuid`.

Обязательные primary keys и constraints:

- `customer_entitlements(customer_id)`; `last_command_number` монотонно
  возрастает и не убывает;
- `quota_periods(quota_period_id)` и не более одного периода с
  `closed_at IS NULL` на customer;
- `node_quota_usage(quota_period_id, node_id)`;
- `vpn_bridge_routes(vpn_fleet_id, routing_key)`;
- `vpn_nodes.desired_revision` положителен и монотонно увеличивается;
- `vpn_accesses(access_id)` и unique
  `(customer_id, kind, logical_target_key, generation)`;
- `vpn_accesses.accounting_id` глобально уникален и не переиспользуется;
- `agent_operations(operation_id)` и unique `(access_id, desired_version)`;
- usage-курсор `(spool_id, sequence)` монотонен внутри `spool_id`;
  `node_usage_cursors(node_id)` хранит подтверждённую позицию на ноду;
- `traffic_usage_items_processed(node_id, spool_id, sequence, accounting_id)`
  обеспечивает начисление item ровно один раз;
- `started_at <= closed_at`, если период закрыт; нулевой исторический период
  допустим при почти мгновенном renewal;
- quota и counters неотрицательны; counters хранятся как `numeric(20,0)`, а
  `total_bytes` вычисляется как `uplink_bytes + downlink_bytes`;
- timestamps используют UTC `timestamptz`;
- исторические access и accounting mappings сохраняются для позднего traffic;
- audit append-only для runtime DB role.

Минимальные runtime indexes сверх primary/unique keys:

- partial unique `quota_periods(customer_id) WHERE closed_at IS NULL`;
- `quota_periods(customer_id, started_at DESC)` для выбора периода по
  `collected_at`;
- partial unique `vpn_accesses(customer_id, kind, logical_target_key) WHERE
  retired_at IS NULL` для единственного текущего access и Customer API;
- partial `vpn_accesses(entry_node_id, access_id) WHERE retired_at IS NULL AND
  desired_state = 'PRESENT'` для node snapshot;
- partial `agent_operations(next_attempt_at, node_id, operation_id)` для retryable
  statuses и `agent_operations(lease_expires_at)` для `IN_FLIGHT`;
- `node_usage_cursors(node_id)` PK — подтверждённая позиция usage-курсора на ноду.

Отдельные `customer_access_blocks`, `node_quota_blocks`, `vpn_credentials`,
`traffic_totals`, `traffic_ingest_usage_batches`, `usage_batch_jobs` и
`traffic_replication_checkpoints` отсутствуют: usage тянется pull'ом от агента
(секция 12), а не через ingest-таблицу или logical replication. Автоматическое
удаление исторических
`customer_entitlements`, `quota_periods`, `node_quota_usage`, retired
`vpn_accesses`, terminal `agent_operations`, `audit_events` и
`traffic_batch_quarantine` в v1 не реализуется. Их будущая архивация или удаление
потребует отдельного изменения спецификации и migrations; `client_uuid` и
`accounting_id` никогда не переиспользуются.

Схема изменяется только versioned SQL migrations. GORM `AutoMigrate` в production
запрещён. Migrations запускаются до application rollout под PostgreSQL advisory
lock; несовместимая schema делает readiness false и не запускает workers.

Эта спецификация описывает **новый** backend и заменяет текущий `internal/vpn/*`
(GORM + прямой Xray-клиент), а не расширяет его; объём работ оценивается как
greenfield, а не как доработка существующего кода.

### 11.1. Транзакционная модель

`customer_entitlements` содержит ровно одну корневую строку на customer. Первый
`ApplyCustomerAccess` создаёт её; последующий Apply обновляет quota либо выполняет
renewal. Все изменения состояния одного customer сначала выполняют
`SELECT ... FOR UPDATE` этой строки. Под этим lock backend сначала сверяет
`command_number` с `last_command_number` и завершает не больший номер
идемпотентным `OK` без изменений. Поэтому renewal, expiry, изменение quota,
manifest materialization и начисление traffic одного customer сериализованы, а
разные customer обрабатываются параллельно.

Нормативный порядок блокировок:

```text
1. customer_entitlements
2. quota_periods
3. node_quota_usage в порядке node_id
4. traffic_usage_items_processed
5. vpn_nodes в порядке node_id
6. vpn_accesses в порядке access_id
7. agent_operations
```

Транзакция пропускает не затрагиваемые ею строки из этого порядка, но
не меняет взаимный порядок фактически получаемых locks. `vpn_nodes` и
последующие строки блокируются usage worker только при первом
пересечении quota.

Для runtime-транзакций используется PostgreSQL `READ COMMITTED` с явными row
locks. `SERIALIZABLE` и distributed locks не требуются. Все транзакции короткие:
JSON parsing, криптография и сетевые вызовы выполняются вне них.

Транзакция, изменяющая состав или desired state users ноды, блокирует её
`vpn_nodes` row и ровно один раз увеличивает `desired_revision` независимо от
числа затронутых access. Если одна транзакция затрагивает несколько нод, их rows
блокируются в порядке `node_id`.

Entitlement-команда в одной транзакции фиксирует entitlement, quota
periods/usage, `exhausted_at`, desired access state и agent operations. Естественная
идемпотентность целевого состояния не позволяет точному повтору Apply создавать
лишние operations, новый quota period или повторный сброс traffic.
Наличие предыдущей `IN_FLIGHT` operation не откладывает durable создание
operation новой desired version, а откладывает только её dispatch.

Usage batch обрабатывается не одной большой транзакцией, а группами
`(customer_id, node_id, quota_period_id)`. Для каждой группы worker блокирует
корневой entitlement и выбранный quota period. Если период уже закрыт,
worker регистрирует ещё не обработанные items с результатом
`IGNORED_CLOSED_PERIOD`, но не изменяет counters, `exhausted_at`, access и
agent operations. Для открытого периода worker дополнительно блокирует
node usage, регистрирует только новые `(spool_id, sequence, accounting_id)`,
увеличивает
total и при первом пересечении порога в той же транзакции устанавливает
`exhausted_at`, переводит access этой ноды в `ABSENT` и создаёт durable
Remove operations. Crash между группами безопасен: committed items не
начисляются повторно, оставшиеся будут продолжены новым worker lease.

Expiry и renewal после блокировки entitlement повторно проверяют актуальные
`expires_at` и quota period. Это исключает удаление доступа
expiry worker после уже committed renewal. При работе с несколькими нодами строки
всегда блокируются по возрастанию `node_id`.

Agent-operation dispatcher берёт operation lease, только если на этой ноде
нет более ранней незавершённой `IN_FLIGHT` operation, и фиксирует lease до RPC.
После commit он загружает актуальный access, проверяет `desired_version`,
расшифровывает `client_uuid` только для `EnsureUserPresent` и выполняет RPC.
Результат записывается отдельной транзакцией с повторной проверкой `desired_version`.
Результат устаревшей operation не меняет `apply_state` актуальной desired
version. Ни одна database transaction не остаётся открытой во время обращения к
node-agent/Xray.

## 12. Traffic accounting через pull от агента

Node-agent каждой ноды читает Xray StatsService (per-email counters) и накапливает
дельты в **локальном durable spool** (SQLite). Backend периодически **тянет** их
через `GetNodeState`; агент в PostgreSQL напрямую не пишет.

Нормативный контракт (из `node_agent.proto`):

```protobuf
rpc GetNodeState(GetNodeStateRequest) returns (GetNodeStateResponse);

message UsageCursor { string spool_id = 1; uint64 sequence = 2; }
message UserUsage   { string accounting_id = 1;
                      uint64 uplink_bytes = 2; uint64 downlink_bytes = 3; }
message UsageBatch  { UsageCursor cursor = 1; int64 collected_at_unix_ms = 2;
                      repeated UserUsage items = 3; }

message GetNodeStateRequest  { UsageCursor acknowledged_usage_through = 1; }
message GetNodeStateResponse { repeated UsageBatch usage = 1;
                               /* health, ActivityBatch — по node_agent.proto */ }
```

`spool_id` — случайный UUID на время жизни локального spool; `sequence` монотонно
растёт внутри `spool_id`. Новый SQLite (переустановка/сброс) создаёт новый
`spool_id` и начинает `sequence` с нуля. Пара `(spool_id, sequence)` глобально
идентифицирует batch и является ключом идемпотентности.

Каждый `UserUsage` — дельта per-email за интервал опроса; `accounting_id` уникален
внутри batch. Raw customer ID, source IP, destination, DNS, SNI, URL и содержимое
пакетов в usage запрещены. `customer_id` в batch нет: несколько FREEDOM/BRIDGE
access одного customer представлены разными accounting items и агрегируются backend.

Agent опрашивает Xray по умолчанию каждые 15 секунд с `reset=true` (единственный
reset owner), пишет ненулевые дельты в spool под новым `sequence` и удаляет
подтверждённые batches. Durable spool переживает рестарт агента; потеря возможна
только при физической утрате spool (новый `spool_id`) — принимаемая погрешность v1.
Размер одного batch ограничен (по умолчанию `<= 5 000` items); больше — делится на
несколько `sequence`.

**Backend-сторона (pull worker).** На ноду одновременно активен один pull worker
(выбирается PostgreSQL lease'ом, как остальные workers). Он периодически вызывает
`GetNodeState(acknowledged_usage_through)`; агент возвращает ещё не подтверждённые
`UsageBatch` в порядке `sequence`. Каждый batch обрабатывается в короткой
PostgreSQL transaction:

1. проверяется монотонность `(spool_id, sequence)`; уже обработанный batch —
   идемпотентный no-op;
2. items сопоставляются с историческими accounting mappings и группируются по
   `(customer_id, node_id, quota_period_id)`;
3. для каждой группы worker блокирует `customer_entitlements`, затем выбранный
   `quota_periods`; если период закрыт — items помечаются `IGNORED_CLOSED_PERIOD`,
   counters/`exhausted_at`/access не меняются;
4. для открытого периода worker блокирует `node_quota_usage`, идемпотентно
   вставляет `traffic_usage_items_processed(spool_id, sequence, accounting_id)`
   через `ON CONFLICT DO NOTHING RETURNING` и добавляет дельты новых items к
   counters; уже обработанный item — no-op;
5. при первом достижении quota в той же transaction worker блокирует `vpn_nodes`,
   затем `vpn_accesses`, ставит `node_quota_usage.exhausted_at`, переводит все
   access customer с данным `entry_node_id` в `ABSENT`, увеличивает node
   `desired_revision` и создаёт durable Remove operations;
6. unknown accounting ID или некорректный item пишутся в quarantine и как
   processed, чтобы не блокировать batch.

Только **после durable commit** batch его cursor подтверждается: следующий
`GetNodeState` передаёт `acknowledged_usage_through`, и агент удаляет подтверждённые
batches из spool. Сетевые agent RPC (Ensure/Reconcile) выполняются вне этих
транзакций.

Дедуп по `(spool_id, sequence, accounting_id)` гарантирует, что повторный pull,
crash pull worker или повтор batch не начисляют item дважды. Смена `spool_id`
трактуется как новый spool: backend продолжает с его `sequence` и старые не
начисляет. Исторический accounting mapping позволяет учитывать items после
expiry/retire.

Quota ноды исчерпана при `node_total_bytes >= usage_quota_bytes`. Конкурентная
обработка нескольких access одного customer на одной ноде сериализуется row lock
`node_quota_usage`, поэтому порог активируется один раз. Item сопоставляется с
периодом по `collected_at_unix_ms` (`started_at <= collected_at` и
`closed_at IS NULL OR collected_at < closed_at`). Если выбранный период уже закрыт
к моменту обработки, item помечается `IGNORED_CLOSED_PERIOD`. Поэтому totals
закрытых периодов могут быть неполными и не являются authority для billing. Renewal
открывает свежий период; расход предыдущего в новую quota не переносится.

Перед bulk removal (expiry/security) агент делает best-effort финальный флаш spool;
ошибка не задерживает removal, поэтому небольшой неучтённый хвост допускается. Для
уже собранных deltas quota — control-plane threshold наилучшего усилия: после
обнаружения превышения node access переходит в `ABSENT`. Потерянная (не успевшая в
spool) delta уменьшает учтённый total и может увеличить фактическое превышение
quota. Этот trade-off принят для v1.

**Activity и health вне scope начисления v1.** `GetNodeState` также возвращает
`health` и `ActivityBatch` с `SourceActivity` (псевдонимный `ip_token`,
`connection_count`; байтов и сырых IP нет); их сигнатуры заданы agent-proto.
Backend v1 для quota читает только `usage`; `health` используется как liveness
ноды (секция 15), а activity в v1 **не enforce-ится** — это задел под будущий
IP-лимит, реюзающий механику `exhausted_at`-блокировки (секция 4).

Дедуп-записи `traffic_usage_items_processed` можно удалять после того, как их cursor
подтверждён и заведомо не будет повторно прислан агентом (spool старше cursor уже
очищен). Очень поздний повтор после такой очистки backend может начислить повторно —
положительная погрешность, принятая для v1. Backlog неподтверждённых cursor'ов на
ноду и quarantined items выносятся в метрики (секция 15). Отдельной ingest-таблицы,
`SECURITY DEFINER` функции и logical replication нет.

## 13. Manifest materialization и масштабирование

Manifest RPC ограничен 4 MiB. Значения по умолчанию:

```text
fleets             <= 100
nodes              <= 100
relations          <= 900
nodes per fleet    <= 10
customers          <= 50 000 (весь парк)
```

Число BRIDGE relations во fleet ограничено `nodes per fleet` — пара
`(entry_node_id, exit_node_id)` уникальна, поэтому при ≤10 нодах на fleet их не
более 90. Число Xray users customer на конкретной ноде равно
`1 + inbound_bridge_relation_count` (один FREEDOM и по одному BRIDGE на каждую
relation с этой входной нодой). Backend не вводит бизнес-ограничение на количество
customer или Xray users на ноде. При `C` customer и `R` входящих BRIDGE relations
нода содержит `C * (1 + R)` управляемых Xray users; фактическая ёмкость
подтверждается инфраструктурным load test, а не admission control в backend.
Превышение остальных технических лимитов требует осознанного изменения
configuration и load test.

Manifest worker обрабатывает customer access пакетами до 500 строк в транзакции.
Глобальная worker concurrency по умолчанию 8; на одну ноду mutating concurrency
равна 1. PostgreSQL leases восстанавливаются после crash. API применяет
backpressure и не создаёт неограниченные in-memory queues.

Изменения manifest:

- новая нода создаёт FREEDOM access и нулевой `node_quota_usage` текущего периода
  всем неистёкшим customer fleet;
- новая relation создаёт BRIDGE access всем неистёкшим customer fleet;
- удаление relation или fleet membership переводит соответствующие access в
  `RETIRED/ABSENT` и создаёт Remove на существующую ноду;
- глобальное удаление ноды переводит установленные на ней access в
  `RETIRED/ABSENT`, supersede-ит pending operations и не создаёт недоставляемые
  agent RPC; факт отсутствия в manifest достаточен и отдельно backend не
  проверяется;
- изменение публичной конфигурации не создаёт agent operations;
- изменение agent endpoint переключает только будущую доставку;
- повторная materialization одной revision идемпотентна.

Отдельный expiry worker выбирает due customer по `expires_at`, используя
`FOR UPDATE SKIP LOCKED`. Он запускается не реже одного раза в 10 секунд. Повторный
запуск и несколько worker replicas не создают дублирующих блокировок или
неограниченного числа Remove operations. Worker выводит причину
`TIME_EXPIRED` непосредственно из `expires_at`, переводит access в `ABSENT` и
создаёт Remove operations в одной транзакции; отдельную block row он не создаёт.

`GetCustomerAccessLinks` дополнительно проверяет `expires_at` по текущему времени
и не возвращает URI после указанной секунды, даже если expiry worker задержался.

## 14. Authentication, authorization и secrets

- product service → backend: mTLS service identity с ролью
  `customer-access-writer`/`customer-access-reader`;
- infrastructure pipeline → backend: отдельная mTLS identity с ролью
  `manifest-writer`;
- backend → node-agent: mTLS поверх management WireGuard overlay, certificate
  identity ноды берётся из manifest; все методы (`EnsureUser*`, `ReconcileUsers`,
  `GetNodeState`, `Health`) идут по этому каналу — node-agent к PostgreSQL и API
  напрямую не обращается;
- PostgreSQL runtime, migration и backup используют разные роли с минимальными
  privileges.

Production secrets не имеют default values и поступают из secret provider или
защищённых файлов. Они не передаются в command-line arguments и не выводятся при
старте. Поддерживается один active encryption key; ротация и decrypt-only keys в
v1 не используются.

## 15. Health, observability и audit

Обязательные endpoints:

```text
GET /health/live
GET /health/ready
GET /metrics
```

Liveness не зависит от PostgreSQL и нод. Readiness требует доступную БД,
совместимую schema и валидный encryption key. Недоступность отдельного агента не
делает весь backend unready.

Structured logs содержат `request_id`, `operation_id`, `node_id` и
стабильный error code, когда применимо. Customer ID допускается только в
ограниченных audit records и не используется как metric label.

Metrics и alerts покрывают:

- возраст и количество agent operations по status;
- agent RPC latency/error и последний успешный вызов по node;
- manifest revision/materialization lag;
- backlog неподтверждённых usage-cursor на ноду, latency `GetNodeState` и
  quarantined events;
- количество `TIME_EXPIRED` customer, исчерпанных node quotas и задержку expiry
  worker;
- количество access по desired/apply state;
- DB pool, migration compatibility и worker leases;
- ошибки расшифрования.

Audit обязателен для customer Apply/renewal/expiry, destructive manifest и
изменения encryption key/configuration.

## 16. Failure semantics

- PostgreSQL недоступен до commit: команда не подтверждается.
- Commit успешен, но ответ потерян: точный повтор Apply возвращает текущее
  эквивалентное состояние и не повторяет side effects.
- Agent недоступен: operation сохраняется и повторяется; остальные ноды работают.
- Agent применил команду, но ответ потерян: декларативная команда повторяется.
- Backend недоступен: существующий VPN-трафик продолжает работать; новые команды и
  recovery ожидают восстановления backend.
- Xray перезапущен: users и routing rules пусты; агент немедленно поднимает их
  add-only из локального снапшота (даже без backend), backend позже сходится
  авторитетно через `ReconcileUsers` (удаления/дрейф).
- Manifest некорректен: projection и customer state не изменяются.
- Один access не применён: он не выдаётся, но READY access того же customer
  продолжают выдаваться.
- Expiry наступил: ссылки перестают выдаваться по сравнению с `expires_at`, даже
  если expiry worker ещё не выполнил database transition; удаления с нод
  доставляются асинхронно.
- Quota достигнута на ноде: её traffic total, `exhausted_at` и Remove operations
  для access этого customer только на данной ноде фиксируются атомарно.
- Pull worker недоступен: usage копится в локальном spool агента; backend наверстает
  при следующем `GetNodeState`; рост неподтверждённого cursor вызывает alert.
- Поздний повтор traffic batch после cleanup dedupe records может повторно
  увеличить total и преждевременно исчерпать quota; это принятая погрешность v1.

Backend не выполняет автоматический failover и не исключает ноды из fleet по
health-проверкам.

Ограничения времени и traffic применяются near-real-time. При expiry нормальная
задержка удаления пользователя с нод включает интервал expiry worker и agent RPC.
При quota она дополнительно включает интервал node-agent sampling и usage-pull
интервал backend.
Удаление Xray user запрещает новые подключения; немедленное завершение уже
установленного соединения в v1 не гарантируется.

## 17. Инварианты v1

- Один customer имеет не более одного fleet.
- Каждая entitlement-команда несёт монотонный `command_number`; backend применяет
  только команду с номером больше сохранённого, поэтому переупорядочивание и
  повтор доставки не искажают итоговое состояние.
- Принятый `vpn_fleet_id` не удаляется и не переиспользуется; fleet может быть пустым.
- Customer получает один FREEDOM access на каждую ноду и один BRIDGE access на
  каждую bridge relation fleet; на одной ноде BRIDGE access может быть несколько.
- Каждый access имеет отдельные `client_uuid` и accounting ID.
- Bridge `client_uuid` устанавливается только на BRIDGE.
- BRIDGE → EXIT credential принадлежит инфраструктуре.
- PostgreSQL является единственным central desired-state authority.
- Manifest является authority топологии и публичной конфигурации.
- `node_id` стабилен, сетевые и публичные параметры изменяемы.
- Готовая VLESS URI не хранится.
- `client_uuid` зашифрован и не попадает в observability/audit metadata.
- Accounting ID не содержит customer identity и не передаётся в URI.
- Agent operations durable, post-commit, ordered per node и идемпотентны.
- Node-agent держит локальный durable spool (usage/activity) и durable snapshot
  backend-owned набора для add-only self-heal при рестарте Xray; авторитетные
  удаления и коррекция дрейфа — только через `ReconcileUsers`.
- Невалидный `ReconcileUsers`-набор не применяется частично; Xray не меняется до
  полной валидации.
- Traffic accounting приблизителен: потерянная delta уменьшает total, а поздний
  retry после cleanup dedupe state может увеличить его повторно.
- Node-agent копит usage в локальном durable spool; backend тянет их `GetNodeState`
  (pull). Node-agent — единственный reset owner Xray counters.
- Traffic item начисляется ровно один раз по `(spool_id, sequence, accounting_id)`,
  пока сохраняются dedupe records: повторный pull, crash pull worker и повтор
  batch не удваивают total. После cleanup этих records поздний retry может быть
  начислён повторно.
- Потеря ещё не сброшенных в spool deltas при сбое агента является принимаемой
  погрешностью v1.
- Quota независимо агрегирует traffic всех access customer на каждой ноде внутри
  явного quota period.
- Renewal открывает свежий quota period; расход предыдущего не переносится. Период
  выбирается по `collected_at` дельты (event-time от агента), поэтому дельты,
  собранные до renewal, всегда сопоставляются со старым (закрытым) периодом,
  отмечаются `IGNORED_CLOSED_PERIOD` и не начисляются ни в старый, ни в новый
  период. Leak в новую quota отсутствует.
- Истечение времени создаёт `ABSENT` для всех нод; достижение quota создаёт
  `ABSENT` только для access соответствующей ноды. История физически не удаляется.
- Backend не обращается к Xray напрямую; агент транслирует команды в Xray
  Handler/Routing API. Per-user routing rule создаёт агент по `egress_key` через
  `RoutingService` (fan-out: один вход → несколько выходов).
- `accounting_id` (Xray email) уникален на access: по нему идут `RemoveUser` и
  per-email статистика.
- Health failure не меняет topology или customer assignment.

## 18. Definition of done

Backend v1 готов к production только когда:

- protobuf customer, manifest и node-agent (`EnsureUser*`, `ReconcileUsers`,
  `GetNodeState`) contracts соответствуют этой спецификации и имеют compatibility
  tests;
- versioned migrations проходят с пустой БД и с каждой поддерживаемой предыдущей
  версией;
- протестированы natural idempotency, lost response, worker crash,
  agent outage и partial fleet readiness;
- переупорядоченные и повторно доставленные команды с меньшим или равным
  `command_number` завершаются `OK` без side effects и не искажают итоговую quota;
- изменение desired state при предыдущей `IN_FLIGHT` operation атомарно
  создаёт новую `PENDING` operation; worker crash до завершения старого
  lease не теряет корректирующую operation;
- manifest validation и destructive guard покрыты integration tests; удаление
  ранее принятого fleet отклоняется и при `allow_destructive=true`;
- полный и пустой `ReconcileUsers`-наборы валидируются до применения; невалидный
  набор не меняет Xray, а ошибка посередине reconcile безопасно исправляется
  повторным reconcile; reconcile восстанавливает и users, и их routing rules;
- per-user routing rule создаётся/удаляется на лету через `RoutingService` без
  reload; после рестарта Xray reconcile восстанавливает users+rules;
- повторный pull, crash pull worker, unknown accounting ID и повтор batch не
  удваивают traffic totals (дедуп `(spool_id, sequence, accounting_id)`);
- поздний retry после cleanup dedupe records может повторно увеличить total и
  проверяется fault test как принятая положительная погрешность;
- потеря spool (новый `spool_id`) проверяется fault test и отражается в documented
  accounting accuracy;
- expiry, независимый quota crossing на двух нодах, несколько access одного
  customer на одной ноде, повышение quota, продление и новый quota period покрыты
  concurrency/integration tests;
- batch закрытого quota period получает `IGNORED_CLOSED_PERIOD`, не
  изменяет historical totals и не блокирует access нового периода;
- `client_uuid` redaction и отсутствие secrets в logs/traces проверяются тестами;
- backup PostgreSQL и encryption key восстановлены на отдельном стенде;
- load test подтверждает configured limits и backpressure;
- production backend больше не использует прямой Xray client и GORM AutoMigrate;
- dashboards и alerts покрывают operations, manifest materialization и usage-pull
  backlog.
