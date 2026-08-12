# Состояние реализации инфраструктуры v1

Дата среза: **12 августа 2026 года**
Рабочая ветка: `feat/infra-v1-foundation`
Статус: **инфраструктурный контур до границы backend/node-agent реализован и проверен офлайн**

## 1. Где мы сейчас

Новая модель инфраструктуры уже существует в исполняемом виде, а не только в
спецификации. Репозиторий умеет загрузить и проверить desired state, построить
типизированную effective-конфигурацию, детерминированно сгенерировать локальные
артефакты и рассчитать влияние изменения относительно явно указанной базы.

Помимо чистого компилятора теперь существует отдельный новый deploy-контур:
Git baseline, manual provisioning preflight, compiled Ansible input, bootstrap,
machine PKI, readiness gates и resume-safe coordinator. Опасные операции
остаются dry-run по умолчанию. Проверенный офлайн coordinator честно
останавливается в `WAITING_FOR_BACKEND`; backend apply, DNS/data-plane promotion
и перемещение deployment ref не изображаются выполненными.

Начат отдельный v1 management-foundation контур. `Platform` descriptor
компилируется в план и два минимальных inventory; операторский playbook может
установить TLS-only Vault с immutable image digest. Vault остаётся доступен
только через loopback и намеренно не инициализируется и не unseal'ится
автоматически. Для временного GitHub-hosted runner добавлен ручной read-only
readiness workflow с GitHub Environment, concurrency lock и обязательной
проверкой pinned `known_hosts` против fingerprints из desired state.

Каталоги `desired/environments/{develop,staging,prod}` по-прежнему содержат
только объекты окружений. Реалистичный develop-флот с placeholder-данными и
только `secret://` references находится в `tests/fixtures/valid/desired`.

## 2. Что реализовано

### 2.1. Desired state и схемы

Создана структура `desired/` для трёх изолированных окружений:

```text
desired/
├── common/
├── environments/{develop,staging,prod}/
└── fleet-ids.yml
```

Поддерживаются пять объектных видов:

- `Environment`;
- `Platform`;
- `Fleet`;
- `LogicalNode`;
- `Instance`.

Для них и для шести секций `desired/common/` созданы строгие JSON Schemas.
Проверяются типы, обязательные поля, допустимые значения, неизвестные поля и
базовые ограничения формата.

Реализована иерархия конфигурации:

```text
desired/common/*
  < Environment.spec.common_overrides
  < LogicalNode.spec.common_overrides
```

Override проходит отдельную строгую схему, mapping-и объединяются рекурсивно,
а вычисляемые значения, например `egress_limit_mbps`, пересчитываются после
merge. No-op override не меняет canonical digest и не создаёт ложного impact.

### 2.2. Валидация

`fleetctl validate` выполняет офлайн:

- загрузку YAML и JSON Schema validation;
- построение типизированной модели;
- разрешение ссылок между fleet, node и instance;
- проверку ролей, членства и bridge-связей;
- проверку изоляции окружений и secret references;
- проверку единственного `serving`-инстанса логической ноды;
- проверку management slots и вычисляемых management-адресов;
- проверку стабильных component digests для нод с трафиком;
- проверку bandwidth profiles и обязательных политик DNS, rollout и Xray;
- лимиты количества флотов, нод, связей и нод на флот.

### 2.3. Компиляция

`fleetctl render` детерминированно создаёт:

```text
build/<environment>/
├── ansible-inventory.json
├── dns-plan.json
├── monitoring-targets.json
└── node-plans/<instance_id>.json
```

Все проекции строятся из одной effective-модели. Environment- и node-level
overrides одинаково учитываются в inventory, node plan, DNS и monitoring.

DNS-план публикует только `serving` entry-инстансы. Monitoring-план включает
все не-retired инстансы, но только `serving` считается SLO-eligible.

### 2.4. Планирование изменений

`fleetctl plan` умеет сравнивать текущее состояние с явно переданным каталогом
baseline и строить semantic impact plan. Уже классифицируются:

- изменения common и node-level overrides;
- добавление, изменение и удаление logical node;
- изменения fleet membership и bridge relations;
- добавление, замена, изменение и удаление instance;
- влияние на provision, configure, runtime, DNS, monitoring и management;
- разрушительные удаления;
- зависимые entry-ноды при замене exit-инстанса.

Canonical representation и digest зависят от effective-значений, а не от
синтаксической формы overlay.

### 2.5. Ограничение исходящего трафика

Реализована роль `node_limits`:

- bandwidth profile выбирается для каждого `Instance`;
- общий egress ceiling рассчитывается из capacity и utilization;
- CAKE применяется на публичном egress и сохраняется через systemd;
- entry использует `dual-dsthost`, exit — `flows`;
- политика проверяется после применения;
- `verify.yml` читает CAKE, conntrack occupancy/capacity, Xray file descriptors,
  CPU, память и threads без изменения kernel или process limits.

Текущий профиль `vps-1g` задаёт 1000 Мбит/с capacity и 90% utilization, то есть
эффективный общий потолок 900 Мбит/с.

### 2.6. Автоматические проверки

На дату обновления проходят:

- 94 unit-теста;
- валидация `develop`, `staging` и `prod`;
- Python bytecode compilation;
- проверка JSON Schema;
- `git diff --check`;
- повторяемость рендера в unit/CI-сценариях;
- Git adapter tests во временных репозиториях;
- bootstrap/PKI shell и YAML проверки;
- coordinator dry-run до `WAITING_FOR_BACKEND` без SSH и внешних мутаций.
- deterministic platform render, generated-input boundary и SSH fingerprint
  matching для GitHub-hosted runtime readiness.

`ansible-core` в текущей локальной среде не установлен, поэтому
`ansible-inventory --list` и `ansible-playbook --syntax-check` здесь пропущены.
Встроенная проверка соответствия inventory/node plans проходит; Makefile
автоматически запускает parser check, когда Ansible доступен.

Live-, contract-, scenario- и нагрузочные тесты намеренно отложены.

### 2.7. Git deployment baseline

- `plan` читает источник и baseline прямо из commit tree без checkout/reset;
- baseline разрешается через `refs/deployments/<environment>` и fail-closed;
- первая раскатка требует явного `--initial`;
- dirty/untracked `desired/` запрещает приписывать состоянию Git SHA;
- impact plan содержит `source_git_sha`, `baseline_git_sha` и
  `initial_deployment`;
- отдельный atomic update-ref использует compare-and-swap guard, planner и
  текущий coordinator его не вызывают.

### 2.8. Compiled Ansible и bootstrap

Generated inventory содержит только connection data и ссылку на один node plan.
Локальный preflight запрещает дополнительные доменные hostvars, проверяет
schema/version, environment/instance binding и соответствие bootstrap/configure
inventories. Новый `playbooks/deploy/configure.yml` не читает `desired/` и не
вычисляет topology. Legacy playbooks сохранены отдельно.

Bootstrap использует отдельный inventory с публичными адресами, создаёт deploy
user, hardening/firewall, Docker prerequisites, node directories, management
WireGuard, CAKE, compiled node plan и machine PKI. Первый прогон может
fail-closed остановиться после выдачи публичного WG-ключа/CSR; после регистрации
peer и подписи CSR прогон возобновляется идемпотентно.

### 2.9. Provisioning, PKI и readiness

Manual provisioning adapter формирует структурированный preflight, проверяет
`provider_resource_id` и глобально маршрутизируемый `public_address`.
`create`/`destroy` возвращают `OPERATOR_ACTION_REQUIRED` и не вызывают provider
API.

Agent- и WireGuard-ключи генерируются локально; роли не читают их через
`slurp`. CA interface отделён от local OpenSSL adapter, который выдаёт
независимый root на environment. Установлены защищённые каталоги, CSR flow,
certificate-chain validation и renewal timer/hook.

Readiness-модель выдаёт для каждого gate `passed`, `diagnostic`, UTC timestamp;
timeout и ошибка всегда являются failure. Покрыты host/management reachability,
systemd, Xray syntax/runtime/listener, CAKE, metrics, certificate, direct exit
smoke и entry-to-exit smoke interface.

### 2.10. Infrastructure coordinator

`fleetctl deploy` реализует последовательность до backend boundary, environment
lock, атомарный deployment record и явный resume. Dry-run является default и не
вызывает Ansible. Даже после успешного apply coordinator возвращает
`WAITING_FOR_BACKEND`, не применяет DNS/backend/data plane и не двигает
deployment ref.

## 3. Что пока отсутствует

Следующие части целевой системы ещё не реализованы:

- backend fleet manifest и проверка его размера до 4 MiB;
- адаптеры `ValidateManifest`, `ApplyFleetManifest` и `GetManifestStatus`;
- `infraagent.v1` и реальный node agent;
- фактическое применение DNS plan и monitoring targets внешними адаптерами;
- продвижение `candidate → serving`, drain и retire;
- защищённый deployment runner;
- автоматическая Vault init/unseal ceremony, GitHub OIDC policy configuration,
  secret resolver и завершённый platform handoff;
- реальные флоты в `desired/environments/*`;
- воспроизводимое построение `develop` с чистых машин;
- продвижение одинаковых component digests в staging и prod.

Подготовлена отдельная явная операция атомарного `update-ref` с compare-and-swap
guard, но coordinator намеренно её не вызывает: ref нельзя двигать до backend,
DNS и data-plane convergence.

Нормативное инфраструктурное ТЗ, вендоренный backend agreement и baseline
`nodeagent.v1` зафиксированы вместе с этим срезом. Контрактную поверхность пока
нельзя считать полной: отсутствуют `infraagent.v1`, точные backend manifest RPC,
authorization matrix и status/idempotency contracts.

## 4. Известные ограничения текущего инкремента

1. `fleetctl plan` по умолчанию использует `refs/deployments/<environment>`;
   `--baseline <desired-directory>` сохранён только как явный тестовый режим.
2. Рендер пока не создаёт `backend-manifest.yaml`; следовательно, часть
   инвариантов, зависящих от манифеста и его размера, ещё не проверяется.
3. Репозиторные environment-файлы пустые с точки зрения флота. Успешная команда
   `fleetctl validate` сейчас подтверждает корректность каркаса, а не готовность
   реальной среды.
4. CAKE автоматизация реализована и покрыта офлайн-тестами, но её влияние на
   throughput/CPU/latency при 50–100 пользователях ещё не измерено.
5. `drain_timeout_seconds` и `degradation_threshold_percent` остаются `null`,
   пока не приняты результаты эксплуатационных и нагрузочных тестов.
6. Conntrack и Xray `nofile` только наблюдаются. Репозиторий намеренно не
   повышает их без измеренного насыщения.
7. `fleetctl deploy` способен вызвать Ansible только с явным `--apply` и тремя
   читаемыми файлами operator inputs. Без флага выполняются только локальные
   шаги; SSH и mutation помечаются `SKIPPED_DRY_RUN`.
8. Platform bootstrap пока только устанавливает неинициализированный Vault.
   Recovery ceremony выполняется оператором; GitHub Actions умеет лишь
   read-only readiness и ещё не получает Vault token через OIDC.

## 5. Следующий порядок работ

1. Установить `ansible-core 2.18` в runner/CI и выполнить обязательные
   `ansible-inventory --list` и syntax-check нового контура.
2. Получить реальные develop-данные из §6 и заменить только placeholder
   develop desired state; staging/prod не заполнять догадками.
3. Провести отдельно разрешённый bootstrap develop VPS, зарегистрировать
   WireGuard peer, подписать CSR и повторить идемпотентный прогон.
4. Дофиксировать backend manifest RPC, `infraagent.v1`, authorization matrix,
   revision/idempotency и status contracts относительно уже вендоренных
   backend agreement и `nodeagent.v1`.
5. После завершения контрактной поверхности реализовать backend manifest,
   fake backend/agent и контрактный стенд.
6. Только после backend convergence добавить DNS/data-plane promotion,
   drain/retire/rollback и вызов guarded deployment-ref update.
7. Провести failure/load/reboot tests и затем продвигать те же component
   digests в staging/prod.

## 6. Данные для первого запуска develop

До подключения к реальным машинам оператор должен предоставить:

- реальные `provider_resource_id` и публичные IPv4/IPv6 entry и exit VPS;
- непредсказуемые публичные hostname/server_name и develop DNS zone;
- immutable image digests Xray, mask, node exporter и остальных применяемых
  компонентов;
- reviewed SSH public keys операторов для deployment user;
- публичный endpoint, management address и публичный WireGuard key хаба;
- значения по всем `secret://` references: REALITY private keys, mask
  certificate/private key и bridge service UUID;
- выбранный CA adapter и безопасное место его environment-scoped state;
- direct-exit и entry-to-exit smoke argv adapters с проверочными
  инфраструктурными credentials;
- отдельно утверждённое разрешение на SSH/bootstrap. DNS, Vault, backend и
  live fleet mutations требуют собственных разрешений и в текущий запуск не
  входят.

## 7. Git-состояние на дату среза

Технические изменения находятся в локальной ветке
`feat/infra-v1-foundation`:

```text
26c1d67  chore: retire legacy deployment entrypoints
fa46cfb  fix: harden bootstrap and deployment resume
27bb73d  feat: add infrastructure deployment coordinator
db5e376  feat: define fail-closed readiness gates
e1737dd  feat: add bootstrap and machine PKI scaffolding
4c56a2c  feat: add manual provisioning preflight
92c688d  feat: drive Ansible from compiled node plans
27e85e3  feat: add fail-closed Git deployment baseline
a7aaec3  chore: scaffold target infrastructure layout
b8e56b8  feat: add desired-state compiler and planner
621e4ff  feat: enforce node egress limits with CAKE
e525dde  ops: register exit-ru and switch default exit
```

Ветка не отправлена в remote. Нормативные документы, backend agreement и
baseline `nodeagent.v1` входят в этот документационный срез; оставшиеся
контрактные поверхности перечислены в §3 и §5.
