# Инфраструктура SpiritVPN v1: как всё устроено

Этот документ — объяснение системы для владельца проекта. Это не нормативная
спецификация и не замена пошаговому runbook. Его задача — дать цельную картину:
где хранится состояние, кто чему доверяет, что делает Ansible, зачем нужен
Vault, как устроена PKI, что происходит при deployment и какие части пока ещё
не реализованы.

Точные требования находятся в
[`INFRA_TECHNICAL_SPEC.md`](../architecture/INFRA_TECHNICAL_SPEC.md), актуальное
состояние реализации — в
[`INFRA_V1_IMPLEMENTATION_STATUS.md`](../status/INFRA_V1_IMPLEMENTATION_STATUS.md),
а команды первого запуска — в [`PLATFORM_BOOTSTRAP.md`](PLATFORM_BOOTSTRAP.md).

## 1. Самая короткая версия

Система строится вокруг четырёх правил:

1. **Git говорит, что должно существовать.** В `desired/` описаны окружения,
   ноды, инстансы, флоты, связи и версии компонентов. Секретных значений там нет.
2. **Vault хранит то, что нельзя положить в Git.** В Git остаются только ссылки
   вида `secret://kv/develop/...#field`.
3. **Management VPS выполняет опасную работу.** Только он читает Vault,
   подключается по SSH к нодам и запускает Ansible. GitHub не получает эти
   возможности.
4. **Runtime доказывает, что желаемое состояние действительно работает.** Для
   этого нужны readiness, метрики и smoke-тесты. Успешный render сам по себе не
   означает успешную раскатку.

Сейчас цепочка реализована до границы backend/node-agent:

```text
Git / pull request
        |
        v
GitHub: validate + test + deterministic render
        |
        | ручной workflow, environment approval, точный Git SHA
        v
management VPS: Git bundle -> fleetctl -> Vault resolver -> Ansible
        |
        v
VPS-ноды: WireGuard + Xray + nginx mask + CAKE + node exporter
        |
        v
readiness
        |
        v
WAITING_FOR_BACKEND
```

`WAITING_FOR_BACKEND` сейчас является честной остановкой, а не ошибкой: manifest
уже строится, но ещё не отправляется backend. DNS не переключается, deployment
ref не двигается, автоматического удаления серверов нет.

## 2. Главные сущности

### Environment

Изолированное окружение: `develop` или `prod`. У каждого собственные:

- management CIDR;
- DNS-зона;
- backend endpoint;
- пути Vault;
- deployment baseline и последовательность manifest revision;
- GitHub Environment и SSH-ключ доступа к management VPS.

Смешивать значения разных окружений запрещено схемами, PKI-идентичностями,
Vault policy и forced-command binding.

### LogicalNode

Логическая VPN-нода, например вход в Нидерландах или выход в Германии. Это
стабильная продуктовая сущность. Её идентичность не должна меняться просто из-за
замены физического VPS.

### Instance

Конкретная машина провайдера, обслуживающая logical node. У неё есть публичный
IP, management-адрес, provider resource ID, lifecycle state и собственная
machine identity.

Разделение `LogicalNode` и `Instance` нужно для безопасной замены сервера:
логическая нода остаётся той же, а физический инстанс, endpoint и сертификат
меняются.

### Fleet

Группа entry/exit-нод, которую видит backend. Fleet содержит связи между entry
и exit. У связи есть стабильный `egress_tag`, по которому node-agent в будущем
будет назначать конкретному клиенту выход.

### Desired state

Набор YAML-файлов в [`desired/`](../../desired/). Это декларация результата, а
не последовательность shell-команд. Мы описываем «должны существовать такие
ноды с такими параметрами», а compiler решает, какие артефакты из этого вывести.

## 3. Что хранится где

| Данные | Где | Почему |
|---|---|---|
| Топология, lifecycle, публичные адреса | Git, `desired/` | Нужны review, история и воспроизводимость |
| Схемы и API-контракты | Git, `contracts/` | Обе стороны должны собираться против одной версии |
| Приватный REALITY key, mask TLS key, bridge UUID | Vault | Это секреты, их нельзя коммитить |
| Agent и WireGuard private keys | Только соответствующая нода | Машинный ключ не должен покидать владельца |
| Vault unseal shares и initial root token | Внешнее recovery-хранилище | Vault не может быть единственным хранилищем ключей от самого себя |
| Manifest revisions и deployment records | Management VPS, `/var/lib/spiritvpn/fleetctl` | Нужны для resume и монотонной нумерации |
| Сгенерированные inventory/node plans | Временный `build/` | Это производные данные, не source of truth |
| Фактическое состояние пользователей | Backend + node-agent/Xray | Это runtime, а не инфраструктурный Git |

В репозитории нельзя хранить приватные ключи, Vault tokens, unseal shares,
готовые клиентские конфигурации и расшифрованные secret-файлы.

## 4. Что делает `fleetctl`

`fleetctl` — собственный компилятор и coordinator инфраструктуры. Это не замена
Ansible: он готовит для Ansible точный и проверенный вход и управляет порядком
всей операции.

Основные стадии:

```text
validate
  -> resolve Git baseline
  -> impact plan
  -> manual provisioning preflight
  -> allocate manifest revision
  -> deterministic render
  -> validate generated Ansible input
  -> bootstrap
  -> configure
  -> readiness
  -> WAITING_FOR_BACKEND
```

### Validate

Проверяет YAML по JSON Schema и дополнительные смысловые инварианты: ссылки,
уникальность ID/адресов, единственный serving instance, корректность bridge,
наличие immutable image digests и соответствие секретных ссылок окружению.

Эта стадия полностью офлайн и не обращается к Vault или серверам.

### Render

Создаёт детерминированные артефакты:

```text
build/<environment>/
├── ansible-inventory.json
├── bootstrap-inventory.json
├── dns-plan.json
├── monitoring-targets.json
└── node-plans/<instance_id>.json
```

Одинаковый desired state должен дать байт-в-байт одинаковый результат. В этих
файлах остаются secret references, но не resolved values.

### Impact plan

Сравнивает выбранный Git SHA с последним успешно развернутым SHA из
`refs/deployments/<environment>`. Plan показывает затронутые инстансы и опасные
удаления. Разрушительное изменение требует отдельного `allow_destructive`.

Deployment ref продвигается автоматически: задача `promote` в `fleet-deploy.yml`
переставляет его после выкатки, но только если запись о развёртывании сообщает
`BACKEND_APPLIED` для той же среды, того же SHA и той же базы, с которой прогон
начинался. Dry-run, apply, не дошедший до бэкенда, и любое расхождение записи с
запросом оставляют ref на месте. Ручной эквивалент — `make fleet-promote`.

`initial=true` нужен только тогда, когда ref ещё не существует, то есть на самой
первой раскатке среды.

### Coordinator state и resume

Coordinator сохраняет запись операции и manifest revision. Повторный запуск
того же SHA без `resume=true` запрещён. Resume проверяет, что SHA, manifest bytes,
digest, revision и destructive flag совпадают с уже зафиксированными.

Dry-run тоже резервирует revision. Поэтому после dry-run применение того же SHA
выполняется с `resume=true`. Пропуски revision допустимы; повторное использование
revision для другого payload — нет.

## 5. Что такое Ansible и зачем он здесь

Ansible — инструмент удалённого приведения серверов к нужному состоянию. Он
подключается по SSH, выполняет модули на машине и старается быть идемпотентным:
повторный запуск не должен ломать уже настроенное состояние или без причины
делать всё заново.

В терминах этого репозитория:

- **inventory** — список машин и адресов подключения;
- **playbook** — сценарий высокого уровня для группы машин;
- **role** — переиспользуемый блок настройки одного компонента;
- **task** — отдельная проверка или изменение;
- **variables** — конкретные значения, передаваемые роли;
- **handler** — действие после изменения, например reload сервиса;
- **become** — выполнение административной части через root/sudo.

### Почему inventory генерируется

Ручной inventory быстро становится вторым source of truth: адрес можно поменять
там и забыть поменять в desired state. В v1 inventory создаётся compiler'ом и
содержит только данные подключения и ссылку на один node plan.

Ansible playbook не читает `desired/` и не вычисляет топологию самостоятельно:

```text
desired YAML -> fleetctl -> node plan -> Ansible roles
```

Это уменьшает вероятность того, что compiler и playbook по-разному поймут одну
и ту же конфигурацию.

### Bootstrap и configure — разные playbook

[`playbooks/bootstrap/bootstrap.yml`](../../playbooks/bootstrap/bootstrap.yml)
работает с чистой машиной по публичному адресу и выполняет:

- установку Docker и базовых пакетов;
- создание deploy user и hardening;
- создание каталогов;
- генерацию WireGuard keypair на ноде;
- подключение к management overlay;
- настройку CAKE;
- генерацию agent key и CSR.

Bootstrap может намеренно остановиться дважды:

1. пока оператор не зарегистрирует публичный WireGuard key на hub;
2. пока CA не подпишет agent CSR и certificate chain не будет передан обратно.

Повтор запуска после этих действий является нормальным resume.

[`playbooks/deploy/configure.yml`](../../playbooks/deploy/configure.yml) работает
по management-адресу и применяет steady-state роли:

- `common`;
- `docker`;
- `node_limits`;
- `xray`;
- `nginx_mask`;
- `node_exporter`;
- `compiled_runtime`.

Все ноды обрабатываются последовательно (`serial: 1`), а ошибка останавливает
весь play (`any_errors_fatal: true`). Это медленнее массовой раскатки, но
консервативно для первого контура.

### Что важно понимать про SSH сейчас

Bootstrap inventory явно использует `root`. Named `deploy` user создаётся как
заготовка для steady state, но окончательная миграция подключения coordinator на
этого пользователя ещё не завершена. На management executor лежит отдельный
Ansible private key из Vault, а доверенные host keys объявлены в desired state
полем `ssh_host_key` инстанса; `known_hosts` компилируется в build-каталог
вместе с инвентарями. `StrictHostKeyChecking=yes` обязателен, и `ssh-keyscan`
не используется нигде: он спрашивает у проверяемого хоста доказательство его же
подлинности.

Ansible check mode полезен, но не является доказательством успешной раскатки:
некоторые команды и реальные сетевые эффекты невозможно полноценно проверить
без запуска. Поэтому после apply всегда нужны readiness и smoke-тесты.

## 6. Vault: что это и как используется

Vault — защищённое хранилище секретов. В текущей схеме он работает на management
VPS, слушает только `127.0.0.1` и недоступен напрямую из интернета, GitHub или
VPN-нод.

### Seal, init и unseal

После установки Vault ещё не инициализирован. При `init` создаются:

- master/recovery material, разделённый на unseal shares;
- initial root token.

В выбранном ручном варианте Vault после перезапуска sealed. Несколько держателей
должны ввести threshold unseal shares. Auto-unseal не используется, потому что
без независимого KMS он только переносил бы секрет рядом с Vault и создавал
ложное ощущение автоматической защиты.

Root token нужен только для редких административных операций: создать mount,
policy/AppRole, импортировать или изменить секрет, сделать snapshot. Его нельзя
оставлять на management VPS или в GitHub.

### KV и `secret://`

Например, desired state содержит:

```text
secret://kv/develop/nodes/develop-entry-nl/reality#private_key
```

Это означает:

- Vault KV mount: `kv`;
- окружение: `develop`;
- path: `nodes/develop-entry-nl/reality`;
- field: `private_key`.

Перед Ansible apply локальный resolver:

1. валидирует desired state;
2. собирает все используемые ссылки;
3. входит в Vault через AppRole своего окружения;
4. читает только `kv/<environment>/*`;
5. создаёт временный Ansible vars-файл и SSH key с правами `0600`;
6. после завершения executor удаляет их.

Resolved secrets не передаются в GitHub, не попадают в generated artifacts и не
должны появляться в Ansible diff/logs.

### AppRole

AppRole — машинная учётная запись management executor. Для каждого environment
она отдельная. Policy разрешает только чтение его KV-префикса, credential
привязан к loopback и не имеет default policy.

AppRole credential долгоживущий и root-owned на management VPS. Это осознанный
компромисс v1: GitHub не получает Vault access, но компрометация root на
management VPS всё равно означает компрометацию deployment-секретов этого
окружения.

### Snapshot

Raft snapshot нужен для восстановления Vault после потери management VPS. Сам
snapshot также секретен и должен быть вынесен в зашифрованное внешнее хранилище.
Отдельно нужно сохранять `/var/lib/spiritvpn/fleetctl`, иначе потеряется история
manifest revisions и безопасный resume.

## 7. PKI: какие сертификаты существуют

PKI — система удостоверяющих центров, ключей и сертификатов. Сертификат связывает
публичный ключ с проверенной identity. Приватный ключ доказывает владение этой
identity и никому не передаётся.

В системе несколько разных видов ключей. Их нельзя считать одним общим
«сертификатом инфраструктуры».

### 7.1. Vault transport TLS

При bootstrap management VPS локально создаёт transport CA и сертификат для
Vault. Они нужны только для шифрования и проверки локального HTTPS-соединения с
Vault.

Это **не** CA node-agent и не CA пользовательского SSH.

### 7.2. Machine/agent identity

На каждой VPN-машине роль `pki_agent`:

1. генерирует EC P-256 private key локально;
2. создаёт CSR;
3. помещает в CSR SPIFFE URI вида
   `spiffe://spiritvpn/develop/instance/develop-entry-nl-01`;
4. отдаёт наружу только CSR;
5. устанавливает возвращённый certificate chain;
6. проверяет identity и срок действия.

`certificate_identity` также входит в backend manifest. В будущем backend будет
сверять не только валидность TLS chain, но и ожидаемую identity конкретного
instance.

Machine PKI пока является scaffolding: Vault PKI mount/issuer и автоматический
CA adapter ещё не подключены к coordinator. Certificate chain должен быть
получен оператором отдельно и добавлен в защищённый bootstrap input. Имеющийся
renewal timer только создаёт сигнал `renewal-required` при приближении срока; он
пока не выпускает новый сертификат самостоятельно.

### 7.3. Backend client identity

По контракту node-agent должен принимать только backend client certificate с
identity вида `spiffe://spiritvpn/<env>/service/backend`. Эта часть станет
рабочей вместе с backend и node-agent. Сейчас trust bundle и runtime
авторизация ещё не завершены.

### 7.4. SSH identities

SSH использует другие ключи:

- operator key — вход человека на management VPS;
- GitHub environment key — вход только в account `github-deploy` с forced
  command;
- Ansible executor key — SSH management VPS к VPN-нодам;
- SSH host key — идентичность самого сервера, закреплённая в `known_hosts`.

GitHub key не даёт shell. Строка в `authorized_keys` жёстко связывает её с одним
environment и root-owned command gate.

### 7.5. WireGuard identities

WireGuard использует собственные asymmetric keys, а не X.509 certificates.
Private key создаётся на ноде, наружу выводится public key. Оператор добавляет
его как peer на management hub. Через overlay затем идут Ansible steady-state и
будущие backend-to-agent gRPC-вызовы.

## 8. GitHub Actions и management executor

GitHub-hosted runner считается оркестратором, но не доверенным местом для
deployment-секретов.

### Обычный CI

На pull request публичные jobs workflow `ci` выполняют проверки fixtures без
ключа расшифровки. На push в `main` дополнительная trusted job на выделенном
runner расшифровывает и проверяет живой desired state:

- schema/semantic validation;
- unit tests;
- deterministic render;
- Ansible inventory parsing и syntax/lint в CI;
- проверки JSON/YAML/templates.

GitHub-hosted jobs не имеют секретов и доступа к серверам. SOPS identity trusted
job хранится только локально на self-hosted runner и не передаётся GitHub.

### Реактивная выкатка: `desired-state-deploy`

Успешный CI точного коммита `main` сам выкатывает `develop`. Никакого отдельного
действия оператора не требуется, и неважно, кто положил коммит: автоматика
релиза, ревьюер или оператор руками. Если CI предыдущего desired-state коммита
был красным, следующий зелёный commit reconciles всё актуальное состояние
`main`, поэтому исправление CI одновременно закрывает накопившийся drift.

`detect` решает, какие контуры затронуты, по путям изменённых файлов:

| Что изменилось | Что выкатывается |
|---|---|
| `desired/environments/<env>/topology.sops.yml` | platform + control + fleet |
| **всё прочее** — `roles/`, `playbooks/`, `fleetctl/`, `contracts/`, `desired/common/`, `scripts/` | platform + control + fleet |

Последняя строка — не оговорка, а правило. Списка «путь → контур» здесь нет
намеренно: он устаревает молча, и ровно так `nodes/` и `fleets/` полгода не
вызывали ничего. Неопознанный путь считается способным задеть что угодно и
выкатывает все три контура. Цена ошибки — холостой прогон с `changed=0`.

Триггер не фильтрует пути: каждый успешный CI `main` запускает reconcile. Это
намеренная плата за fail-safe поведение — даже commit, исправляющий только CI,
повторит ранее заблокированную выкатку. Неопознанный путь консервативно включает
все три develop-контура; их идемпотентность превращает ненужный проход в no-op.

Порядок — platform → control → fleet, по зависимостям: хаб несёт Vault и
исполнителя, из которых работают две другие выкатки. Пропущенное звено
пропускает дальше, упавшее — останавливает цепь.

`prod` через этот путь не проходит **никогда**: он отсекается в `detect`, потому
что гейта одобрения не существует (GitHub Environments недоступны на плане
free). Prod выкатывается только ручным `workflow_dispatch`.

Для **новой** ноды достаточно объявления в `nodes/`, `fleets/`, `instances/`:
выкатка запустится сама, нода забутстрапится сама. Host key нового VPS
объявляется там же, полем `ssh_host_key` инстанса, и `known_hosts` компилируется
из desired state вместе с инвентарями — рукописных файлов в пути флота не
осталось.

### Ручной `fleet-deploy`

Оператор указывает:

- environment;
- полный 40-символьный SHA из `main`;
- `dry-run` или `apply`;
- `initial`;
- `resume`;
- `allow_destructive`.

GitHub Environment должен потребовать approval. Workflow проверяет, что SHA
достижим из `main`, забирает текущий deployment ref при его наличии и создаёт
Git bundle. В bundle находятся Git objects, но нет Vault secrets.

Через restricted SSH bundle поступает на management VPS. Forced command
проверяет, что environment соответствует SSH-ключу, и не принимает произвольный
shell. Root executor дополнительно проверяет refs/SHA, делает изолированный
checkout и запускает `fleetctl`.

### Почему Git bundle, а не `git pull` на management VPS

Так management VPS не нужен GitHub token или repository deploy key. GitHub
передаёт ровно уже проверенный source SHA и baseline. Executor не выбирает
самостоятельно «последнюю ветку», которая могла измениться между review и apply.

## 9. Что появляется на VPN-ноде

После успешных bootstrap/configure на машине должны быть:

- hardened host baseline;
- Docker и Docker Compose;
- management WireGuard;
- CAKE qdisc для общего ограничения исходящей полосы;
- Xray;
- nginx mask для REALITY camouflage;
- node exporter;
- agent private key, CSR/certificate chain и renewal timer;
- защищённые каталоги runtime.

### Xray

Xray обслуживает VLESS/REALITY data plane. Infrastructure владеет базовой
конфигурацией, transport, static bridge credentials и outbound'ами. Customer
users должны добавляться только node-agent через loopback API.

Xray API в compiled contour слушает напрямую на `127.0.0.1:10085`, потому что
у него нет собственной надёжной application authentication. Публичный доступ к
нему не нужен. Служебный `tunnel` inbound для API запрещён: при потере runtime
route он замыкает порт на самого себя и исчерпывает ephemeral TCP ports ноды.

Включены `HandlerService`, `RoutingService`, `StatsService` и
`ReflectionService`. Container health вызывает реальный `StatsService`, а не
только синтаксическую проверку файла. Изменение startup-конфигурации обязательно
перезапускает Xray и ожидает успешный API healthcheck.

### nginx mask

REALITY маскируется под обычный TLS-сайт на loopback nginx. Сертификат и ключ
маски приходят из Vault через secret references.

### CAKE

CAKE ограничивает суммарный egress машины и даёт справедливое распределение
потоков. Профиль берётся из desired state. Это защита от насыщения порта и
неуправляемой очереди, а не пользовательская quota.

### Node exporter

Экспортирует host-метрики. Сам факт установки exporter ещё не означает, что
центральный Prometheus уже собирает его: monitoring targets генерируются, но
активный v1 adapter их применения пока отсутствует.

## 10. Backend и node-agent

### Backend

Backend/PostgreSQL runtime уже описывается в `Environment.spec.control` и
разворачивается workflow `control-deploy` локально на management VPS. Образы
backend, migrations и PostgreSQL фиксируются digest; среды изолированы Compose
project, data path, secrets и WireGuard management address. Runtime существует,
но gRPC adapter отправки fleet manifest пока не реализован.

Будущая успешная граница deployment:

```text
ApplyFleetManifest -> APPLIED или IDEMPOTENT
```

Это означает, что backend durable принял manifest. Создание agent operations и
их доставка происходят асинхронно. Их задержка контролируется метриками и
alert'ами, а не удерживает deployment RPC открытым.

### Node-agent

Node-agent будет gRPC-сервером на каждой entry-ноде и единственным владельцем
runtime customer users Xray. Он должен:

- добавлять/удалять пользователя;
- создавать персональное routing rule;
- делать authoritative reconcile;
- восстанавливать Xray после рестарта из локального snapshot;
- собирать per-user traffic counters;
- хранить durable usage spool при недоступном backend;
- отдавать health, inventory и Prometheus metrics.

Agent реализован в отдельном репозитории и подключён в infra как digest-pinned
Compose service. State хранится в `/var/lib/spirit-agent`, TLS private key
остаётся node-local, а readiness проверяет running service, `/health/ready` и
gRPC listener. Live rollout пока не выполнен: реальные ноды, image digest и
подписанные machine certificates ещё нужно подготовить.

## 11. Readiness, мониторинг и alerts

Readiness — ответ на вопрос «можно ли считать конкретную раскатку рабочей прямо
сейчас?». Monitoring — постоянное наблюдение после раскатки. Это разные вещи.

### Реализованные readiness gates

Проверяются, среди прочего:

- management address/interface;
- systemd units;
- Xray config syntax и running container;
- публичный listener;
- активная CAKE policy;
- node exporter response;
- agent certificate identity и lifetime;
- direct-exit и entry-to-exit smoke adapters.

Smoke commands должны быть предоставлены оператором. Пустой обязательный adapter
даёт failure, а не фиктивный успех.

### Что пока не работает как полная система мониторинга

- generated `monitoring-targets.json` ещё не применяется внешним adapter;
- v1 observability stack не развёртывается новым coordinator;
- agent metrics target генерируется, но центральный scraper ещё не применён;
- backend materialization/operation dashboards и alerts не применены;
- DNS/data-plane probes не включены в автоматическое promotion.

Центральный observability adapter ещё не реализован; отдельного альтернативного
пути развёртывания в репозитории нет.

После появления agent минимально нужны alerts на:

- agent/Xray unavailable;
- `needs_bootstrap`;
- stale usage collection;
- растущий или переполненный usage spool;
- ошибки self-heal;
- materialization lag backend;
- старые pending/retryable/fatal agent operations;
- истекающие certificates;
- насыщение CAKE/conntrack/file descriptors.

## 12. Как выглядит первый запуск

### Шаг 1. Подготовить management VPS

Оператор вручную создаёт VPS, независимо получает его SSH host key и заполняет:

- SOPS-зашифрованный bundle
  `inventories/bootstrap/platform.sops.yml`, содержащий inventory, pinned
  `known_hosts` и bootstrap vars без plaintext IP/ключей в Git.

В bootstrap vars входят public WireGuard peers операторов. Management VPS
считается чистым: роль сама устанавливает WireGuard, локально создаёт hub key,
настраивает адреса develop/prod и только после этого включает firewall.
IP ноутбука не является входным параметром.

Затем с рабочего компьютера запускает platform check и bootstrap. Это единственный
этап, где новый management VPS настраивается напрямую оператором.

### Шаг 2. Провести Vault ceremony

Оператор вручную выполняет:

1. `status`;
2. `init`;
3. распределение unseal shares/root token во внешнее recovery-хранилище;
4. несколько `unseal`;
5. `configure develop`;
6. импорт требуемых secrets;
7. snapshot и его вынос с VPS.

### Шаг 3. Подготовить executor

Platform bootstrap автоматически создаёт первоначальный root-owned
`bootstrap.yml` с endpoint и public hub key. Оператор дополняет certificate
chains после получения CSR. На management VPS используются:

```text
/etc/spiritvpn/deploy/develop/bootstrap.yml
/etc/spiritvpn/deploy/develop/readiness.yml
```

`known_hosts` в этом списке больше нет: он компилируется из desired state в
build-каталог исполнителя. Host key ноды объявляется полем `ssh_host_key` её
инстанса.

AppRole credentials создаёт `configure develop`. Ansible private key лежит в
Vault как `secret://kv/develop/executor/ansible#private_key`.

### Шаг 4. Настроить GitHub Environment

Для `develop` задаются:

- `PLATFORM_SSH_PRIVATE_KEY`;
- `PLATFORM_SSH_HOST`;
- required reviewer/environment approval.

Ключ должен совпадать с environment-bound public key, установленным platform
bootstrap.

### Шаг 5. Заполнить реальный desired state

Нужно внести реальные VPS, IP, provider IDs, hostnames, immutable image digests,
fleet topology и secret references. Сейчас tracked environment-каталоги являются
пустым каркасом, поэтому «validation passed» ещё не означает наличие флота.

### Шаг 6. Выполнить dry-run

В GitHub запускается `fleet-deploy` для точного SHA:

```text
mode=dry-run
initial=true
resume=false
allow_destructive=false
```

Нужно проверить impact plan и deployment record. Dry-run не подключается к
VPN-нодам, но резервирует manifest revision.

### Шаг 7. Выполнить apply/resume

Для того же SHA:

```text
mode=apply
initial=true
resume=true
allow_destructive=false
```

WireGuard peer регистрируется на management hub автоматически. На первых
прогонах нормальна остановка для подписи agent CSR. После ручного действия
запускается resume того же SHA.

Coordinator передаёт Ansible `--limit`: bootstrap получает только новые
инстансы, configure/readiness — только множество `affected` из impact plan.
При пустом impact Ansible не вызывается. Повторный `resume` не запускает уже
завершённый этап, а одинаковый Compose/config state не пересоздаёт контейнеры.

### Шаг 8. Зафиксировать развёрнутое

После успешного Ansible/readiness координатор передаёт манифест бэкенду и запись
доходит до `BACKEND_APPLIED`. Deployment ref после этого переставляет задача
`promote` — сама, из того же прогона. Оператору делать ничего не нужно.

Ручное перемещение остаётся только для прогонов, доведённых руками:

```bash
make fleet-promote ENVIRONMENT=develop APPLY=1 \
  SOURCE_GIT_SHA=<40 hex> BASELINE_GIT_SHA=$(git ls-remote origin refs/deployments/develop | cut -f1)
```

Изображать завершение по-прежнему нельзя: `--expected-baseline-git-sha` называет
прежнее значение явно, и без него compare-and-swap вырождается в перезапись.
Если запись о развёртывании не дошла до `BACKEND_APPLIED`, ref двигать нечем.

## 13. Как будет выглядеть обычная эксплуатация

После завершения backend/agent частей предполагаемый цикл такой:

1. изменение desired state в отдельной ветке;
2. pull request и полностью офлайн CI;
3. review impact/security;
4. merge в `main`;
5. ручной dry-run точного SHA;
6. environment approval;
7. apply/resume на management VPS;
8. readiness;
9. backend manifest `APPLIED/IDEMPOTENT`;
10. наблюдение materialization lag;
11. отдельное безопасное DNS/data-plane promotion, если оно требуется;
12. guarded update deployment ref.

Из этого списка не реализован только пункт 11: отдельного DNS/data-plane
promotion нет. Пункт 6 недостижим — approval через GitHub Environment на плане
free отсутствует, поэтому prod отсекается в `detect`, а develop reconciles после
успешного CI точного коммита `main`.

## 14. Как система ведёт себя при отказах

| Ситуация | Поведение |
|---|---|
| Ошибка схемы/desired state | Deployment не начинается |
| SHA не из `main` | GitHub workflow отказывает |
| GitHub develop key пытается вызвать prod | Forced command отказывает |
| Vault sealed | Readiness/apply с secret resolution отказывает |
| Secret отсутствует | Resolver отказывает до Ansible |
| SSH host key не совпал | SSH отказывает, `ssh-keyscan` fallback отсутствует |
| Два deployment одного environment | Environment lock отказывает второму |
| Повтор SHA без `resume` | Coordinator отказывает |
| Resume с другим manifest | Coordinator отказывает |
| Нода недоступна | Ansible/readiness отказывает, backend/DNS/ref не меняются |
| Backend/agent ещё отсутствуют | Статус остаётся `WAITING_FOR_BACKEND` |
| Vault перезапустился | Нужен ручной threshold unseal |
| Потерян management VPS | Нужны Vault snapshot, recovery material и backup fleetctl state |

Fail-closed означает, что отсутствие доказательства успеха трактуется как
неуспех. Система предпочитает остановиться, а не угадать или продолжить с
неполным состоянием.

## 15. Что уже готово, а что нет

| Область | Текущее состояние |
|---|---|
| Desired state schemas/compiler | Готово |
| Git baseline и impact planning | Готово |
| Generated Ansible inventory/node plans | Готово |
| Bootstrap/configure/readiness playbooks | Готово офлайн, live-прогон ещё нужен |
| Manual provisioning preflight | Готово; provider create/destroy ручные |
| CAKE | Готово офлайн, нужен нагрузочный тест |
| Vault installation/TLS/manual ceremony | Готово офлайн, нужен живой bootstrap/restore drill |
| Vault secret resolver/AppRole | Готово офлайн |
| GitHub protected handoff | Готово офлайн, GitHub Environments ещё нужно настроить |
| Backend/PostgreSQL runtime | Готово офлайн; нужны digests, Vault secrets и live apply |
| Backend manifest compiler/revisions | Готово, RPC не отправляется |
| Node-agent runtime | Готово офлайн; нужен live rollout |
| Machine PKI issuance/renewal automation | Только scaffolding |
| Xray `RoutingService` | Ещё не включён |
| Central monitoring/alerts | Plans есть, применение не готово |
| DNS/data-plane promotion | Не готово |
| Deployment ref advancement | Намеренно не вызывается |
| Drain/retire/automatic destroy | Не готово |

## 16. Практическая памятка

Безопасные локальные команды:

```bash
make fleet-validate
make fleet-test
make fleet-render ENVIRONMENT=develop
make fleet-ansible-check ENVIRONMENT=develop
make fleet-provisioning-check ENVIRONMENT=develop
```

Проверка platform bootstrap inputs:

```bash
make fleet-platform-check
```

Установка management foundation требует явного `APPLY=1`:

```bash
make fleet-platform-bootstrap APPLY=1 \
  PLATFORM_BUNDLE=inventories/bootstrap/platform.sops.yml
```

Главные данные для резервного копирования:

```text
Vault unseal shares и root recovery material — раздельное внешнее хранение
Vault Raft snapshots                       — зашифрованное внешнее хранение
/var/lib/spiritvpn/fleetctl                — protected backup
/etc/spiritvpn/deploy                      — protected backup или воспроизводимое восстановление
inventories/bootstrap/platform.sops.yml    — Git, только SOPS ciphertext
```

Перед любой реальной операцией полезно задать себе четыре вопроса:

1. Какой точный Git SHA я применяю?
2. Какой environment и какой deployment baseline используются?
3. Где находятся требуемые secrets и кто получит к ним доступ?
4. Какой runtime-сигнал докажет успех и что останется неизменным при отказе?

Если на четвёртый вопрос нет ответа, операцию пока нельзя считать
автоматизированной.
