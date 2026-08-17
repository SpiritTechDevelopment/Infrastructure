# Состояние реализации инфраструктуры v1

Дата среза: **15 августа 2026 года**
Рабочая ветка: `feat/infra-v1-foundation`
Статус: **management foundation развёрнут; control runtime и NodeAgent готовы офлайн, live rollout не выполнен**

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

Точный `spiritvpn.manifest.v1` protobuf-контракт завендорен из backend commit
`91326dad33678e30344904c75e7cff17621bc455`. Реализован чистый compiler полного
manifest snapshot с deployment-scoped revision, строгим destructive guard,
локальным payload digest и лимитом 4 MiB. Coordinator уже выделяет и pin'ит
revision и manifest identity, но намеренно не выполняет gRPC-вызов.

V1 management foundation развёрнут на отдельном management VPS. Двухфазный
bootstrap автоматически установил WireGuard до hardening, перевёл операторский
доступ на management overlay, установил Docker, loopback-only Vault с TLS и
ограниченные executors. Контрольный повтор завершился с `changed=0` и
`failed=0`, то есть live management-контур сошёлся идемпотентно.

Vault инициализирован и unsealed вручную. Включены KV v2, AppRole и file audit;
созданы отдельные `fleet-deployer-develop` и `fleet-deployer-prod` policies и
локальные AppRole credentials. Secret import, ограниченная operator policy,
snapshot/restore и reboot/unseal drill ещё не выполнены.

Выделенный self-hosted runner зарегистрирован, GitHub Environment SSH secrets
подготовлены, а management-хост принимает только environment-bound forced
commands. End-to-end запуск workflows из `main` ещё не проверен: текущая ветка
не отправлена в remote и MR не создан.

Поддерживаются ровно две среды: `develop` и `prod`. Их каталоги пока содержат
только объекты окружений. Реалистичный develop-флот с placeholder-данными и
только `secret://` references находится в `tests/fixtures/valid/desired`.

Добавлен отдельный environment-bound `control-deploy`: на одном management VPS
он изолирует `develop` и `prod`, запускает digest-pinned PostgreSQL, matching
migration image и backend, проверяет readiness и хранит successful release
marker. Добавлена роль NodeAgent с immutable image, persistent SQLite,
node-local mTLS и readiness. Реальные digests, control secrets и certificates
ещё не внесены, поэтому live deployment этих компонентов не выполнялся.

## 2. Что реализовано

### 2.1. Desired state и схемы

Создана структура `desired/` для двух изолированных окружений:

```text
desired/
├── common/
├── environments/{develop,prod}/
└── fleet-ids.yml
```

Поддерживаются четыре объектных вида:

- `Environment`;
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
- разрешение временной ноды без fleet membership для безопасного двухфазного
  decommission; членство более чем в одном fleet запрещено;
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
├── control-plan.json        # только если Environment.spec.control задан
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
- readiness playbook читает CAKE, conntrack occupancy/capacity, Xray file descriptors,
  CPU, память и threads без изменения kernel или process limits.

Текущий профиль `vps-1g` задаёт 1000 Мбит/с capacity и 90% utilization, то есть
эффективный общий потолок 900 Мбит/с.

### 2.6. Автоматические проверки

На дату обновления проходят:

- 138 unit-тестов;
- валидация `develop` и `prod`;
- Python bytecode compilation;
- проверка JSON Schema;
- `git diff --check`;
- повторяемость рендера в unit/CI-сценариях;
- Git adapter tests во временных репозиториях;
- bootstrap/PKI shell и YAML проверки;
- coordinator dry-run до `WAITING_FOR_BACKEND` без SSH и внешних мутаций.
- fail-closed bootstrap inventory preflight и shell tests restricted GitHub
  command gate;
- offline reference listing, environment-bound Vault policy и `0600` resolver
  output checks.

Локальное `ansible-env` содержит `ansible-core`; реальные inventory parsing,
playbook syntax-check и `ansible-lint` проходят. Management bootstrap дополнительно
проверен на живом хосте с повторным convergence apply.

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
inventories. `playbooks/deploy/configure.yml` не читает `desired/` и не
вычисляет topology. Альтернативный ручной deployment-контур удалён.

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
deployment ref. Перед рендером coordinator атомарно выделяет environment-scoped
manifest revision, фиксирует identity точных rendered bytes в record и при
resume требует полного совпадения.

### 2.11. Backend manifest contract и compiler

Зафиксирован точный wire-контракт `ManifestService.ApplyFleetManifest`.
Компилятор строит полный детерминированный request из verified desired state и
того же impact plan: ноды используют единственный serving instance, management
endpoint и machine identity; fleets получают append-only `vpn_fleet_id`, полный
`node_ids` и стабильные bridge `egress_tag`. Secret references и приватные
значения в manifest не попадают.

`revision` проверяется как положительный `uint64`. Destructive request требует
одновременно обнаруженного destructive impact и отдельного явного
`allow_destructive=true`; избыточное разрешение для non-destructive plan также
отклоняется. Plan digest обязан совпадать с рендеримым desired state, поэтому
подмена состояния после review закрывается fail-closed.

Allocator хранит отдельную монотонную последовательность каждого окружения в
`.fleetctl-state/manifest-revisions/` по умолчанию. На management executor
задаётся root-owned `--state-dir /var/lib/spiritvpn/fleetctl`. Allocation привязан
к deployment ID, source Git SHA, local payload digest и destructive-флагу.
Deployment record фиксирует revision, payload digest, SHA-256 и размер
`backend-manifest.json`; сам эфемерный manifest воспроизводится из Git. Потеря
state после pin, повреждение record или несовпадение повторного рендера
останавливают resume. Dry-run также резервирует revision; пропуски разрешены
backend-контрактом.

Официальная граница deployment — ответы `APPLIED` и `IDEMPOTENT`.
Materialization и доставка agent operations асинхронны и должны наблюдаться
backend-метриками и алертами; manifest v1 не содержит Validate/Status RPC.

### 2.12. Operator bootstrap и GitHub handoff

Оператор вручную выполняет Vault init/unseal, environment policy/AppRole setup,
initial secret import и, когда будет введено резервное копирование, Raft
snapshot через root-owned команду. Root token и unseal shares не сохраняются на
VPS; auto-unseal без независимого KMS не реализован. AppRole разрешает только
read под `kv/<environment>/*`, привязан к loopback и не получает default policy.

`fleet-deploy` workflow принимает полный SHA, достижимый из `main`, и передаёт
source плюс deployment baseline как Git bundle. Forced command проверяет
environment binding и строгие аргументы. Management executor проверяет bundle,
изолированно materialize'ит commit, резолвит секреты во временные файлы `0600`,
запускает coordinator с pinned host keys и удаляет временные значения. GitHub не
получает Vault token, AppRole, fleet SSH key или resolved secrets.

### 2.13. Backend/PostgreSQL и NodeAgent runtime

`Environment.spec.control` типизированно фиксирует backend source SHA, точные
digests backend/migration/PostgreSQL, DB roles, backup policy, authorization и
только environment-scoped Vault references. Compiler выпускает локальный
`control-plan.json`; изменение control release попадает только в
`impact-plan.affected.control` и не вызывает передеплой fleet-нод.

`control-deploy` принимает только SHA из `main`, проверяет одно-refовый Git
bundle и выполняется root-owned executor на management VPS. Секреты разрешаются
из loopback Vault во временный `0600` файл и удаляются после запуска. PostgreSQL
не публикует host port; среды используют разные Compose projects и data paths.
Migration запускается только при изменении release. Для существующего prod DB
перед миграцией обязательны локальный dump и настроенный внешний backup adapter;
неявная смена PostgreSQL major version запрещена.

NodeAgent включён в compiled runtime на fleet-нодах. Он использует pinned image,
host networking, постоянный `/var/lib/spirit-agent`, node-local private key и
environment-bound backend SPIFFE identity. Readiness проверяет service,
`/health/ready` и TCP listener. Совпадающий Compose definition не
пересоздаётся принудительно.

## 3. Что пока отсутствует

Следующие части целевой системы ещё не реализованы:

- protobuf/gRPC mTLS adapter для `ApplyFleetManifest` и его интеграция с
  coordinator;
- автоматическая выдача/ротация backend↔client и backend↔NodeAgent mTLS;
- применение materialization/agent-operation dashboards и alerts из
  нормативного backend observability contract;
- фактическое применение DNS plan и monitoring targets внешними адаптерами;
- продвижение `candidate → serving`, drain и retire;
- реальные флоты в `desired/environments/*`;
- воспроизводимое построение `develop` fleet-нод с чистых машин;
- продвижение одинаковых component digests из `develop` в `prod`.

Подготовлена отдельная явная операция атомарного `update-ref` с compare-and-swap
guard, но coordinator намеренно её не вызывает. В целевом контуре ref можно
двигать после `APPLIED`/`IDEMPOTENT`; data-plane promotion замены остаётся
отдельной защищённой операцией и не выводится из одного backend ответа.

Нормативное инфраструктурное ТЗ, backend agreement, точный `manifest.v1` и
baseline `nodeagent.v1` зафиксированы вместе с этим срезом. Для исполняемого
контрактного стенда всё ещё отсутствуют `infraagent.v1`, manifest gRPC adapter
и автоматизированная PKI/authorization ceremony.

## 4. Известные ограничения текущего инкремента

1. `fleetctl plan` по умолчанию использует `refs/deployments/<environment>`;
   `--baseline <desired-directory>` сохранён только как явный тестовый режим.
2. Coordinator уже выделяет revision и рендерит `backend-manifest.json`, но
   `backend_apply.status` остаётся `NOT_SENT`: сетевого adapter ещё нет.
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
8. Vault init/unseal и environment AppRole setup выполнены вручную. Secret
   import остаётся текущей operator ceremony. Snapshots осознанно отложены;
   auto-unseal без независимого KMS отсутствует, snapshot restore и
   reboot/unseal ещё не проверены.
9. Revision state локален management executor и требует резервного копирования.
   До первого реального Apply к уже существующему backend понадобится отдельная
   проверенная процедура seed/recovery от последней принятой backend revision;
   молчаливый старт с `1` для непустого backend запрещён.

## 5. Следующий порядок работ

1. Отправить foundation-ветку, провести MR и проверить `platform-readiness` и
   `platform-deploy` end-to-end из `main` для `develop` и `prod`.
2. Заменить использование initial root token ограниченной operator policy и
   отозвать root token до загрузки runtime-секретов.
3. Реализовать SOPS-encrypted desired state, чтобы fleet topology и public IP
   не появлялись в Git plaintext.
4. Получить реальные develop-данные из §6 и заполнить только `develop`.
5. Провести отдельно разрешённый bootstrap develop VPS, зарегистрировать
   WireGuard peer, подписать CSR и повторить идемпотентный прогон.
6. Заполнить control desired state/digests, выпустить mTLS и проверить сначала
   `control-deploy(check)`, затем live `develop` apply.
7. Добавить protobuf/gRPC mTLS adapter, fake backend и контрактный стенд только
   для `ApplyFleetManifest`; до live apply определить seed/recovery revision
   state для непустого backend.
8. Завершать deployment и guarded deployment-ref update по
   `APPLIED`/`IDEMPOTENT`; отдельно реализовать backend materialization alerts.
9. Добавить защищённый DNS/data-plane promotion замены, drain/retire/rollback;
   до машинного сигнала reconcile эта операция остаётся ручной.
10. Провести failure/load/reboot tests и затем продвигать те же component
    digests из `develop` в `prod`.

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
