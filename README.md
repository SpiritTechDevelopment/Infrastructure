# SpiritVPN Infrastructure v1

Этот репозиторий описывает, проверяет и разворачивает инфраструктуру SpiritVPN.
Он содержит desired state флотов, компилятор `fleetctl`, Ansible-роли, bootstrap
management-платформы и GitHub Actions для дальнейших деплоев.

Текущий статус: management-платформа развёрнута, повторный bootstrap сходится
без изменений, Vault инициализирован и настроен для `develop` и `prod`.
Реализованы digest-pinned runtime backend/PostgreSQL на management VPS и
NodeAgent на fleet-нодах, но они ещё не заполнены реальными release/secrets и
не проверены live. GitHub deploy flow ещё не проверен end-to-end, реальные
VPN-флоты не созданы.

## С чего начать?

Если репозиторий открыт впервые, порядок такой:

1. Прочитать раздел [«Как устроена система?»](#как-устроена-система).
2. Подготовить локальное окружение по разделу
   [«Как запустить проверки?»](#как-запустить-проверки).
3. Для первого management VPS использовать
   [«Как выполнить первичный bootstrap?»](#как-выполнить-первичный-bootstrap).
4. После bootstrap провести отдельную
   [Vault ceremony](#как-инициализировать-vault).
5. Описывать изменения через `desired/` и обычный Git flow.

Подробная нормативная спецификация находится в
[INFRA_TECHNICAL_SPEC.md](docs/architecture/INFRA_TECHNICAL_SPEC.md), а актуальные
ограничения — в
[INFRA_V1_IMPLEMENTATION_STATUS.md](docs/status/INFRA_V1_IMPLEMENTATION_STATUS.md).

## Как устроена система?

В системе есть два разных объекта управления.

### Management-платформа

Это отдельный VPS, на котором размещаются:

- management WireGuard hub;
- Docker;
- Vault;
- защищённые локальные deployment executors;
- постоянное состояние координатора деплоя.

На этом же VPS могут независимо работать `develop` и `prod` control stacks:
для каждой среды создаются отдельные Compose project, PostgreSQL data directory,
секреты и management IP (`10.80.0.1` либо `10.82.0.1`). PostgreSQL наружу не
публикуется. Процессы backend и PostgreSQL внутри контейнеров непривилегированные;
отдельный публичный SSH-доступ backend-серверам не нужен.

Его первоначальная установка выполняется оператором с ноутбука. После bootstrap
обычные изменения management-компонентов должны приходить из GitHub по
ограниченному SSH-интерфейсу и быть привязаны к точному Git commit.

### VPN-флоты

Это entry- и exit-ноды с Xray и вспомогательными компонентами. Их желаемое
состояние должно описываться в `desired/`, после чего `fleetctl`:

```text
desired state
    → валидация
    → компиляция
    → impact plan
    → Ansible inventory и node plans
    → bootstrap только новых машин
    → reconciliation только затронутых машин
    → readiness
```

Локальные проверки и планирование не обращаются к сети. Изменение серверов
требует явного `APPLY=1` или `--apply`.

## Что является источником истины?

| Данные | Источник истины |
|---|---|
| Архитектура и границы ответственности | [техническая спецификация](docs/architecture/INFRA_TECHNICAL_SPEC.md) |
| Желаемая топология флотов | [`desired/`](desired/) |
| Формат desired state | [`contracts/desired-state/`](contracts/desired-state/) |
| Контракт backend | [`contracts/backend/`](contracts/backend/) |
| Контракт node agent | [`contracts/nodeagent/`](contracts/nodeagent/) |
| Bootstrap management VPS | SOPS-файл [`platform.sops.yml`](inventories/bootstrap/platform.sops.yml) |
| Секретные значения приложений | Vault |
| Результат компиляции | `build/`, только временный артефакт |
| Наблюдаемое здоровье | runtime-проверки и будущий monitoring adapter |

`build/` не редактируется вручную и не коммитится. Fleet inventory также не
пишется вручную: он генерируется из validated desired state.

## Что находится в директориях?

| Путь | Назначение |
|---|---|
| [`desired/`](desired/) | Редактируемое человеком желаемое состояние сред и флотов |
| [`contracts/`](contracts/) | JSON Schema, backend contract и protobuf-контракт node agent |
| [`fleetctl/`](fleetctl/) | Валидатор, компилятор, planner и deployment coordinator |
| [`inventories/bootstrap/`](inventories/bootstrap/) | Единственный ручной inventory, целиком закрытый SOPS |
| [`playbooks/platform/`](playbooks/platform/) | Bootstrap и steady-state management-платформы |
| [`playbooks/control/`](playbooks/control/) | Локальный deploy backend и PostgreSQL на management VPS |
| [`playbooks/bootstrap/`](playbooks/bootstrap/) | Первичная установка чистых fleet-нод |
| [`playbooks/deploy/`](playbooks/deploy/) | Приведение fleet-нод к compiled node plan |
| [`playbooks/operations/`](playbooks/operations/) | Readiness-проверки fleet-нод |
| [`roles/`](roles/) | Идемпотентные Ansible-роли компонентов |
| [`scripts/`](scripts/) | Ограниченные операторские entrypoint и служебные утилиты |
| [`.github/workflows/`](.github/workflows/) | CI и защищённые GitHub deployment workflows |
| [`tests/`](tests/) | Unit-тесты и synthetic fixtures |
| [`docs/`](docs/) | Архитектура, runbook и статус реализации |

Основные группы ролей:

- `platform_wireguard`, `platform_vault`, `platform_executor` — management VPS;
- `bootstrap_wireguard`, `common`, `docker` — базовая установка машин;
- `control_runtime` — PostgreSQL, migrations, backend и readiness выбранной среды;
- `compiled_node_plan`, `compiled_runtime` — применение скомпилированного плана;
- `xray`, `nginx_mask`, `node_agent`, `node_exporter`, `node_limits`, `pki_agent`
  — компоненты fleet-ноды.

## Как подготовить локальное окружение?

Потребуются Git, Make, Python 3, `sops` и `wireguard-tools`. Для изменяющего
bootstrap также нужны `sudo`, `ip` и рабочий SSH-доступ к management VPS.

Python/Ansible окружение создаётся локально и игнорируется Git:

```bash
python3 -m venv ansible-env
source ansible-env/bin/activate
python -m pip install -r requirements-ansible.txt

export ANSIBLE_LOCAL_TEMP=/tmp/spiritvpn-ansible-local
```

Версия Ansible должна соответствовать ограничению в
[`requirements-ansible.txt`](requirements-ansible.txt):

```bash
ansible --version
```

Для расшифровки bootstrap bundle оператору нужна age identity, соответствующая
публичному recipient из [`.sops.yaml`](.sops.yaml). Приватная age identity
хранится вне репозитория, обычно в стандартном хранилище SOPS.

## Как подготовить management host?

Management host и Actions runner — разные VPS. На management host до bootstrap
нужны только поддерживаемая Debian/Ubuntu, доступ `root` по SSH-ключу и
независимо проверенный SSH host key. Docker, WireGuard, Vault и служебные
пользователи устанавливаются из этого репозитория.

### 1. Создать operator SSH key

Если отдельного операторского ключа ещё нет:

```bash
umask 077
ssh-keygen -t ed25519 -a 100 -N '' \
  -f ~/.config/spiritvpn/keys/operator-management \
  -C spiritvpn-operator-management
```

Public half нужно установить в `/root/.ssh/authorized_keys` чистого VPS через
панель провайдера или другой уже доверенный канал. Private half в Git не
попадает. Перед Ansible preflight загрузите ключ в `ssh-agent` либо явно задайте
его для Ansible:

```bash
ssh-add ~/.config/spiritvpn/keys/operator-management
# либо:
export ANSIBLE_PRIVATE_KEY_FILE="$HOME/.config/spiritvpn/keys/operator-management"
```

### 2. Закрепить SSH host key

Host key — это идентичность самого VPS, а не ключ пользователя. Получите public
host key через консоль провайдера или другой независимый канал:

```bash
cat /etc/ssh/ssh_host_ed25519_key.pub
```

Если чистый образ не создал Ed25519 host key, его можно один раз создать на VPS:

```bash
ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
chown root:root /etc/ssh/ssh_host_ed25519_key{,.pub}
chmod 600 /etc/ssh/ssh_host_ed25519_key
chmod 644 /etc/ssh/ssh_host_ed25519_key.pub
systemctl restart ssh
```

Public host key вместе с management-адресом будет зашифрован внутри SOPS
bundle. Private host key никогда не покидает VPS.

### 3. Создать operator WireGuard key

На каждом операторском устройстве создаётся собственная пара:

```bash
umask 077
wg genkey > ~/.config/spiritvpn/keys/operator-wg.key
wg pubkey < ~/.config/spiritvpn/keys/operator-wg.key
```

В SOPS попадает только выведенный public key и зарезервированные `/32` адреса.
Один оператор может получить адрес в нескольких средах.

### 4. Создать environment-bound GitHub SSH keys

Для `develop` и `prod` используются разные пары:

```bash
umask 077
ssh-keygen -t ed25519 -a 100 -N '' \
  -f ~/.config/spiritvpn/keys/github-develop \
  -C spiritvpn-github-develop

ssh-keygen -t ed25519 -a 100 -N '' \
  -f ~/.config/spiritvpn/keys/github-prod \
  -C spiritvpn-github-prod
```

Private halves загружаются в соответствующие GitHub Environments. Public halves
записываются в SOPS bundle и после bootstrap получают разные forced-command
bindings.

### 5. Заполнить SOPS bootstrap bundle

Сначала должен быть известен public IP runner VPS: финальный firewall разрешает
ему restricted SSH. Редактируйте только через SOPS:

```bash
sops inventories/bootstrap/platform.sops.yml
```

После расшифровки структура должна иметь следующий вид. Значения ниже —
шаблоны; реальные адреса и ключи вводятся только в открывшийся редактор SOPS:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: PlatformBootstrap
inventory: |
  all:
    children:
      spiritvpn_platform_bootstrap:
        hosts:
          management-1:
            ansible_host: MANAGEMENT_PUBLIC_IP
            ansible_user: root
known_hosts: |
  MANAGEMENT_PUBLIC_IP ssh-ed25519 MANAGEMENT_HOST_PUBLIC_KEY
vars: |
  platform_operator_ssh_public_keys:
    - ssh-ed25519 OPERATOR_PUBLIC_KEY
  platform_github_ssh_keys:
    - environment: develop
      public_key: ssh-ed25519 GITHUB_DEVELOP_PUBLIC_KEY
    - environment: prod
      public_key: ssh-ed25519 GITHUB_PROD_PUBLIC_KEY
  platform_ssh_allowed_cidrs:
    - RUNNER_PUBLIC_IP/32
  platform_fail2ban_ignore_cidrs:
    - RUNNER_PUBLIC_IP/32
    - 10.80.0.0/16
    - 10.82.0.0/16
  platform_vault_node_id: management-1
  platform_vault_tls_server_name: vault.management.internal
  platform_wireguard_hub_addresses:
    develop: 10.80.0.1/16
    prod: 10.82.0.1/16
  platform_wireguard_operator_peers:
    - id: operator-name
      public_key: OPERATOR_WIREGUARD_PUBLIC_KEY
      allowed_ips:
        - 10.80.255.254/32
        - 10.82.255.254/32
```

Правила:

- в inventory разрешены только `ansible_host` и `ansible_user`;
- management host должен быть ровно один;
- `platform_ssh_allowed_cidrs` — источники прямого SSH после hardening, обычно
  только runner VPS;
- операторам public SSH не нужен: они входят через WireGuard;
- WireGuard `/32` каждого оператора уникальны и находятся внутри CIDR среды;
- `known_hosts` содержит именно независимо проверенный host key;
- после сохранения поля `inventory`, `known_hosts` и `vars` должны снова стать
  `ENC[AES256_GCM,...]`.

Проверка без подключения:

```bash
make fleet-platform-check
```

При добавлении оператора, GitHub environment key или нового runner CIDR bundle
редактируется повторно, коммитится и применяется повторным platform bootstrap.
Private keys при этом не ротируются автоматически.

## Как создать или добавить self-hosted runner?

Текущие deployment workflows ожидают repository-level runner с labels:

```text
self-hosted, linux, spiritvpn-deploy
```

Runner должен находиться на отдельном Debian/Ubuntu VPS. Ему нужны исходящий
TCP/443 к GitHub и исходящий TCP/22 к management host. Входящее соединение от
GitHub не требуется.

Для проверки зашифрованной topology runner получает отдельную локальную age
identity. Это не identity management executor и не recovery key; приватная
часть не хранится в GitHub Secrets. Создание выполняет ручной workflow
`runner-sops-bootstrap`, который печатает только публичный recipient.

### 1. Получить версию и digest

Откройте в GitHub репозитории **Settings → Actions → Runners → New self-hosted
runner → Linux**. Возьмите оттуда точную версию и SHA256 архива для архитектуры
VPS. Скрипт намеренно не использует плавающий `latest`.

### 2. Передать bootstrap-скрипт

```bash
scp scripts/bootstrap-self-hosted-runner.sh root@RUNNER_IP:/root/
```

### 3. Зарегистрировать runner

Короткоживущий registration token передаётся по pipe и не сохраняется в файл:

```bash
set -o pipefail
gh api \
  --method POST \
  repos/OWNER/REPOSITORY/actions/runners/registration-token \
  --jq .token |
ssh root@RUNNER_IP \
  '/root/bootstrap-self-hosted-runner.sh \
    --repository-url https://github.com/OWNER/REPOSITORY \
    --runner-version X.Y.Z \
    --runner-sha256 64_HEX_CHARACTERS \
    --runner-name spiritvpn-deploy-1 \
    --labels spiritvpn-deploy'
```

### 4. Проверить runner

На VPS:

```bash
cd /opt/actions-runner
./svc.sh status
id github-runner
```

В GitHub runner должен отображаться как `Idle`. Пользователь `github-runner` не
должен входить в группы `sudo`, `docker`, `lxd` или `wheel`.

### Как добавить второй runner?

Повторите процедуру на другом VPS с уникальным `--runner-name` и тем же label
`spiritvpn-deploy`. GitHub сможет назначить deployment job любому свободному
runner с этим label. Добавьте public IP нового runner как отдельный `/32` в
`platform_ssh_allowed_cidrs`, затем повторно примените management bootstrap.

Одинаковый label не разделяет среды. Для строгого разделения `develop` и `prod`
нужны разные labels и изменение `runs-on` в workflows. Один общий persistent
runner сейчас является временным компромиссом.

## Как запустить проверки?

Полная локальная проверка:

```bash
make check
make lint
make fleet-platform-check
```

Она проверяет:

- схемы `develop` и `prod`;
- unit-тесты `fleetctl` и защитных границ;
- синтаксис shell и Python;
- синтаксис активных Ansible playbook;
- YAML и Ansible lint;
- структуру и содержимое расшифрованного SOPS bundle.

Проверка реального SSH-доступа к management VPS без изменения сервера:

```bash
make fleet-platform-bootstrap-check CONNECT=1
```

Успешный результат заканчивается Ansible `ping: pong` и `changed: false`.

Все эти проверки объединены в одном безопасном entrypoint:

```bash
scripts/platform-bootstrap.sh
```

Без `--apply` он никогда не запускает bootstrap.

## Как выполнить первичный bootstrap?

Bootstrap предполагает чистый management VPS. До запуска нужны:

- независимо проверенный SSH host key;
- временный root SSH-доступ;
- публичные SSH-ключи операторов в SOPS bundle;
- отдельные GitHub SSH public keys для `develop` и `prod` в SOPS bundle;
- локальный WireGuard private key оператора;
- соответствующий ему public key в SOPS bundle;
- открытый у VPS-провайдера входящий UDP-порт WireGuard, по умолчанию `51820`;
- чистое рабочее дерево Git с зафиксированным commit.

Запуск:

```bash
source ansible-env/bin/activate
scripts/platform-bootstrap.sh --apply
```

Сценарий сначала повторяет все безопасные проверки, показывает SHA исходного
commit и требует вручную ввести `APPLY`. Затем он:

1. Подключается к management VPS по закреплённому SSH host key.
2. Устанавливает management WireGuard до включения финального firewall.
3. Генерирует private key WireGuard непосредственно на VPS.
4. Создаёт управляемый `/etc/wireguard/spiritvpn-mgmt.conf` на ноутбуке.
5. Проверяет SSH через внутренний WireGuard-адрес.
6. Только после успешного туннеля применяет hardening и firewall.
7. Устанавливает Docker, Vault и restricted executors.
8. Повторяет Ansible apply для проверки сходимости.
9. Проверяет локальный интерфейс `spiritvpn-mgmt`.

Роль `common` является единственным владельцем входной firewall-политики:
она сначала применяет таблицу `inet spiritvpn_filter`, затем отключает и
удаляет UFW и persistent legacy-правила, а оставшиеся IPv4/IPv6 `INPUT`-цепочки
переводит в пустой `ACCEPT`. Docker-owned `FORWARD`/NAT-цепочки при этом не
очищаются. Это не позволяет старой INPUT-политике с `DROP` молча перекрыть
корректные правила management overlay после reboot или повторного apply.

Сценарий откажется перезаписывать чужой WireGuard config. Если туннель не
поднялся, основная фаза hardening не начинается.

Версии Vault и общего observability-стека не задаются в defaults ролей или в
локальном runtime bundle хаба. Их единственный источник — SOPS-зашифрованный
`desired/common/components.yml`. Bootstrap и `platform-deploy` расшифровывают
этот файл только на доверенном контроллере, проверяют schema и обязательные
digest, материализуют временные Ansible variables с mode `0600` и удаляют их
после прогона. Отсутствующий pin завершает deployment до изменения compose.

Параметры management WireGuard — имя интерфейса, сети сред, listen port и MTU —
также не имеют рабочих fallback в роли. Они принадлежат SOPS-зашифрованному
`inventories/bootstrap/platform.sops.yml`. Git-проектор и operator refresh
формируют из одного набора полей полный runtime contract; несовпадающий
сохранённый contract останавливает обычный apply до Ansible.

Низкоуровневый entrypoint, используемый сценарием:

```bash
make fleet-platform-bootstrap APPLY=1
```

Его следует использовать только для диагностики. Обычный операторский путь —
`scripts/platform-bootstrap.sh --apply`.

## Как инициализировать Vault?

Bootstrap запускает доступный только локально Vault, но намеренно не выполняет
`init`, `unseal` и запись секретов.

Сначала нужно подготовить внешнее recovery-хранилище. Unseal shares и initial
root token нельзя сохранять:

- в Git или SOPS этого репозитория;
- в GitHub Secrets;
- на management VPS;
- в `.local-secrets`;
- в shell history или обычном файле ноутбука.

Проверка состояния с management-хоста:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator status
```

Пример политики из пяти shares с threshold три:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator init 5 3
```

JSON с shares и root token выводится один раз. Значения нужно немедленно
разнести по утверждённому внешнему recovery-хранилищу. `5/3` — пример, а не
универсальная политика организации.

Каждый из трёх держателей вводит свою share интерактивно:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator unseal
```

После unseal для каждой используемой среды создаётся отдельная policy и
loopback-bound AppRole:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator configure develop
sudo /usr/local/sbin/spiritvpn-vault-operator configure prod
```

Команды запрашивают временный root token интерактивно. GitHub его не получает.
Полная ceremony описана в
[PLATFORM_BOOTSTRAP.md](docs/operations/PLATFORM_BOOTSTRAP.md).

## Как положить прикладной секрет в Vault?

В `desired/` хранится только ссылка вида:

```text
secret://kv/develop/example/component#field
```

Список ссылок, необходимых конкретной среде, можно получить без чтения Vault:

```bash
python3 scripts/vault-secret-resolver.py \
  --root . \
  --environment develop \
  --list-references
```

Значение записывается на management VPS интерактивно:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator \
  put develop 'secret://kv/develop/example/component#field'
```

После приглашения значение передаётся через standard input и завершается
`Ctrl-D`. Команда запрещает запись ссылки одной среды в другую.

## Как описывать инфраструктуру?

Модель состоит из пяти связанных объектов:

```text
Environment
  └── Fleet
      ├── entries[] ──→ LogicalNode(role=entry) ──← Instance
      ├── exits[]   ──→ LogicalNode(role=exit)  ──← Instance
      └── bridges[] ──→ entry → exit
```

- `Environment` задаёт границу `develop` или `prod`.
- `Fleet` — продуктовая группа, к которой привязывается клиент.
- `LogicalNode` — стабильная VPN-идентичность: домен, REALITY и роль.
- `Instance` — конкретный VPS, исполняющий логическую ноду.
- `Bridge` — явная направленная связь одной entry-ноды с одной exit-нодой.

Удаление VPS не должно менять `LogicalNode`. При замене создаётся новый
`Instance`, а домен, REALITY identity и bridge routing key сохраняются.

### Шаг 0. Учесть видимость topology

Текущий `desired/` читается compiler напрямую как обычный YAML. Поэтому реальные
`public_address`, состав флота и связи будут видны всем, кто читает Git
репозиторий. Ни private keys, ни UUID туда всё равно не попадают — только
`secret://` ссылки.

Если даже в приватном Git topology должна быть ciphertext, сначала нужен
отдельный SOPS input/materialization contour для `desired/`. Он ещё не
реализован. Следующий пример показывает существующий plaintext-контракт.

### Шаг 1. Закрепить общие параметры и image digests

Общие параметры находятся в `desired/common/`. До объявления первой traffic
node validator требует ненулевые immutable digest как минимум для `xray`,
`nginx_mask`, `alloy` и `node_exporter`:

```yaml
components:
  xray:
    repository: ghcr.io/xtls/xray-core
    tag: 26.3.27
    digest: sha256:REPLACE_WITH_64_LOWERCASE_HEX
```

Digest должен относиться к проверенному образу и нужной архитектуре. Тег нужен
для чтения человеком, но runtime использует `repository@sha256:...`.

Проверьте также:

- `desired/common/limits.yml` — существует ли нужный bandwidth profile;
- `desired/common/networking.yml` — WireGuard, agent port и DNS policy;
- `desired/common/rollout.yml` — таймауты и последовательность rollout;
- `desired/common/observability.yml` — локальные порты и интервалы;
- `desired/common/xray.yml` — стабильные Xray tags.

Переопределения задаются только через `spec.common_overrides`. Приоритет:

```text
desired/common/* < Environment.spec.common_overrides < LogicalNode.spec.common_overrides
```

Например, увеличить convergence timeout только для `develop`:

```yaml
spec:
  # остальные поля Environment
  common_overrides:
    rollout:
      convergence_timeout_seconds: 600
```

Произвольные Ansible variables в `desired/` запрещены, а неизвестные поля
отвергаются схемой.

### Шаг 2. Настроить Environment

Среды расположены в:

```text
desired/environments/develop/
desired/environments/prod/
```

Расшифрованный `Environment` внутри
`desired/environments/develop/topology.sops.yml` имеет вид:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: Environment
metadata:
  id: develop
spec:
  dns_zone: develop.example.invalid
  management_network: 10.80.0.0/16
  backend_endpoint: backend.develop.internal:9443
  secret_store:
    kv: kv/develop
    pki: pki/develop
```

Management CIDR закреплены валидатором:

| Среда | CIDR |
|---|---|
| `develop` | `10.80.0.0/16` |
| `prod` | `10.82.0.0/16` |

`dns_zone`, `backend_endpoint` и placeholder-домены нужно заменить реальными
значениями до эксплуатации.

#### Описать backend и PostgreSQL среды

Control stack задаётся в том же `Environment.spec.control`. В Git хранятся
только commit SHA, immutable image digests, несекретные имена ролей и
`secret://`-ссылки. Полный проверяемый пример находится в
[`tests/fixtures/valid/desired/environments/develop/environment.yml`](tests/fixtures/valid/desired/environments/develop/environment.yml).

Минимальная структура:

```yaml
spec:
  # dns_zone, management_network, backend_endpoint и secret_store — как выше
  control:
    backend_release:
      source_git_sha: REPLACE_WITH_40_CHARACTER_BACKEND_COMMIT
      backend_image:
        repository: ghcr.io/spirittechdevelopment/spiritvpn-spiritvpnd
        digest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
      migration_image:
        repository: ghcr.io/spirittechdevelopment/spiritvpn-migrate
        digest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
    postgres:
      image:
        repository: docker.io/library/postgres
        digest: sha256:REPLACE_WITH_REVIEWED_POSTGRES_DIGEST
      major_version: 17
      database: spiritvpn
      owner_user: spiritvpn_owner
      runtime_user: spiritvpn_runtime
      backup_required: false # для prod должно быть true
      # Для prod — абсолютный argv проверенного внешнего backup adapter.
      # Credentials adapter получает из Vault или собственного root-only файла.
      external_backup_command_argv: []
    secrets:
      postgres_owner_password_ref: secret://kv/develop/control/postgres#owner_password
      postgres_runtime_password_ref: secret://kv/develop/control/postgres#runtime_password
      migration_database_url_ref: secret://kv/develop/control/postgres#migration_database_url
      runtime_database_url_ref: secret://kv/develop/control/postgres#runtime_database_url
      client_uuid_key_ref: secret://kv/develop/control/backend#client_uuid_key
      grpc_tls_certificate_ref: secret://kv/develop/control/backend-tls#server_certificate
      grpc_tls_private_key_ref: secret://kv/develop/control/backend-tls#server_private_key
      grpc_tls_client_ca_ref: secret://kv/develop/control/backend-tls#clients_ca
      agent_tls_certificate_ref: secret://kv/develop/control/backend-tls#agent_client_certificate
      agent_tls_private_key_ref: secret://kv/develop/control/backend-tls#agent_client_private_key
      agent_tls_ca_ref: secret://kv/develop/control/backend-tls#agents_ca
    authorization:
      customer_access_writers: [spiffe://spiritvpn/develop/service/customer-service]
      customer_access_readers: [spiffe://spiritvpn/develop/service/customer-service]
```

`backend_image` и `migration_image` обязаны быть собраны из одного
`source_git_sha`. Digest — это неизменяемый SHA-256 идентификатор содержимого
образа, а не тег. `prod` и `develop` могут использовать одинаковый порт 9443:
Compose привязывает их к разным WireGuard-адресам management VPS.

Список требуемых значений Vault выводится без обращения к Vault:

```bash
python3 scripts/vault-secret-resolver.py \
  --root . \
  --environment develop \
  --scope control \
  --list-references
```

Каждое значение загружается интерактивно; оно не попадает в аргументы процесса
или Git:

```bash
ssh -t deploy@10.80.0.1 \
  'sudo /usr/local/sbin/spiritvpn-vault-operator put develop secret://kv/develop/control/postgres#owner_password'
```

Для PostgreSQL нужны два независимых пароля и две DSN на Compose hostname
`postgres:5432`: migration DSN использует `owner_user`, runtime DSN —
`runtime_user`. Для backend также нужны его UUID encryption key и две стороны
mTLS: server identity для manifest clients и client identity для NodeAgent.

Обе стороны mTLS выпускает `fleetctl`. Имена он берёт из desired state, а не
из рук оператора: DNS-имя серверного сертификата — это host из
`backend_endpoint`, а идентичность клиентского — та же строка, которую
компилятор кладёт в `SPIRIT_GRPC_ALLOWED_CLIENT_IDENTITIES` на нодах.

```bash
make fleet-pki-issue ENVIRONMENT=develop PROFILE=backend-server CA_STATE=~/.config/spiritvpn/ca
make fleet-pki-issue ENVIRONMENT=develop PROFILE=backend-client CA_STATE=~/.config/spiritvpn/ca
```

`CA_STATE` обязателен и умолчания не имеет: корневой ключ среды не должен
попасть ни в репозиторий, ни в `generated/`. Команда печатает, какой файл в
какое поле Vault кладётся, — при одном корне на среду один и тот же `ca.crt`
идёт и в `clients_ca`, и в `agents_ca`.

Сертификат ноды через эти команды не выпускается: его приватный ключ рождается
на самой ноде (`roles/pki_agent`), наружу уходит только CSR, который
подписывается `make fleet-pki-sign INSTANCE=... CSR=...`.

#### Телеграм-бот рядом с бэкендом

`spec.control.bot` — необязательное поддерево. Среда, где его нет, бота не
разворачивает; среда, где он есть, получает его на том же management-хосте, что
и бэкенд, тем же `control-deploy`.

Бот делит с бэкендом инстанс PostgreSQL, но не базу и не роли: `control_runtime`
заводит ему отдельную базу и две отдельные учётные записи внутри того же
контейнера. Для бэкенда бот — обычный внешний клиент: ходит по mTLS личностью
`customer-service`, которая обязана быть перечислена и в
`customer_access_writers`, и в `customer_access_readers`.

```bash
make fleet-pki-issue ENVIRONMENT=develop PROFILE=customer-service CA_STATE=~/.config/spiritvpn/ca
```

Наружу мини-апп публикуется исходящим туннелем Cloudflare: `cloudflared` в
compose дозванивается до Cloudflare сам, поэтому на management-хосте не
открывается ни одного входящего публичного порта. Токен туннеля кладётся в
`tunnel_token_ref`, публичное имя объявляется в `ingress.hostname`, а
`subscription_base_url` и `mini_app_url` компилятор выводит из него — вторая
копия имени разошлась бы с тем, что туннель на самом деле публикует.

Список секретов бота выводится тем же `--list-references --scope control`, что и
для бэкенда. Две DSN бота обязаны нести драйвер (`postgresql+asyncpg://`): бот
работает через асинхронный движок SQLAlchemy и на голом `postgresql://` не
стартует. Миграции гоняются `alembic upgrade head` из того же образа, что и сам
бот, поэтому схема и код приезжают одной парой.

Создайте каталоги объектов:

```bash
mkdir -p \
  desired/environments/develop/fleets \
  desired/environments/develop/nodes \
  desired/environments/develop/instances
```

### Шаг 3. Добавить логические entry и exit

Entry `desired/environments/develop/nodes/develop-entry-nl.yml`:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: LogicalNode
metadata:
  id: develop-entry-nl
spec:
  role: entry
  region: nl
  public:
    hostname: edge-a7.develop.example.invalid
    port: 443
    transport: tcp
    flow: xtls-rprx-vision
    fingerprint: chrome
    server_name: edge-a7.develop.example.invalid
  reality:
    public_key: REPLACE_WITH_ENTRY_REALITY_PUBLIC_KEY
    short_id: 0123abcd
    private_key_ref: secret://kv/develop/nodes/develop-entry-nl/reality#private_key
  mask:
    certificate_ref: secret://kv/develop/nodes/develop-entry-nl/mask#fullchain
    private_key_ref: secret://kv/develop/nodes/develop-entry-nl/mask#private_key
  display_name: Netherlands
```

Exit `desired/environments/develop/nodes/develop-exit-de.yml` имеет ту же
структуру, но другую identity:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: LogicalNode
metadata:
  id: develop-exit-de
spec:
  role: exit
  region: de
  public:
    hostname: edge-b4.example.invalid
    port: 443
    transport: tcp
    flow: xtls-rprx-vision
    fingerprint: chrome
    server_name: edge-b4.example.invalid
  reality:
    public_key: REPLACE_WITH_EXIT_REALITY_PUBLIC_KEY
    short_id: 4567cdef
    private_key_ref: secret://kv/develop/nodes/develop-exit-de/reality#private_key
  mask:
    certificate_ref: secret://kv/develop/nodes/develop-exit-de/mask#fullchain
    private_key_ref: secret://kv/develop/nodes/develop-exit-de/mask#private_key
  display_name: Germany
```

Правила LogicalNode:

- ID: `<environment>-<role>-<region>` и имя файла совпадает с ID;
- entry hostname обязан принадлежать `Environment.spec.dns_zone`;
- hostname нельзя напрямую выводить из node ID;
- REALITY public key и `short_id` открытые, private key — только ссылка на Vault;
- TLS mask certificate и private key — только ссылки на Vault;
- REALITY identity принадлежит LogicalNode и сохраняется при замене VPS.

Текущий репозиторий не автоматизирует выпуск mask-сертификата и REALITY
keypair. Их нужно создать доверенным внешним процессом, записать public half в
YAML, а private material — в Vault.

### Шаг 4. Описать конкретные VPS как Instance

Entry instance `desired/environments/develop/instances/develop-entry-nl-01.yml`:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: Instance
metadata:
  id: develop-entry-nl-01
spec:
  logical_node: develop-entry-nl
  target_state: serving
  public_address: 192.0.2.10
  bandwidth_profile: vps-1g
  provider:
    name: manual
    resource_id: provider-resource-entry-01
```

Exit instance `desired/environments/develop/instances/develop-exit-de-01.yml`:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: Instance
metadata:
  id: develop-exit-de-01
spec:
  logical_node: develop-exit-de
  target_state: serving
  public_address: 192.0.2.20
  bandwidth_profile: vps-1g
  provider:
    name: manual
    resource_id: provider-resource-exit-01
```

Адреса `192.0.2.0/24` являются документационными: замените их public IP
реальных VPS. `provider.resource_id` — неизменяемый ID ресурса у провайдера, а не
его отображаемое имя.

Management address вручную не задаётся. Он выводится из role и числового suffix:

```text
develop-entry-nl-01 → 10.80.1.11
develop-exit-de-01  → 10.80.2.11
```

Suffix `01…240` — постоянный slot, уникальный внутри `(environment, role)`.
Освобождённый slot нельзя немедленно переиспользовать для другой машины.

Жизненный цикл instance:

| `target_state` | Значение |
|---|---|
| `provisioning` | VPS создан, bootstrap ещё не завершён |
| `candidate` | Установлен и проверяется без клиентского трафика |
| `serving` | Текущий обслуживающий VPS логической ноды |
| `draining` | Трафик уже переключён, старые соединения доживают |
| `retired` | Удалён из runtime, VPS можно уничтожать после проверки |

У каждой LogicalNode валидатор требует ровно один `serving` instance. Во время
замены рядом с ним может существовать `candidate`, затем старый становится
`draining` и `retired`.

### Шаг 5. Создать Fleet и постоянный числовой ID

Добавьте append-only запись в `desired/fleet-ids.yml`:

```yaml
develop-fleet-eu: 1
```

После первого успешного применения значение нельзя менять, удалять или
переиспользовать для другого флота.

Файл `desired/environments/develop/fleets/develop-fleet-eu.yml`:

```yaml
apiVersion: spiritvpn.io/v1alpha1
kind: Fleet
metadata:
  id: develop-fleet-eu
spec:
  entries:
    - develop-entry-nl
  exits:
    - develop-exit-de
  bridges:
    - routing_key: develop-entry-nl.to-develop-exit-de
      entry: develop-entry-nl
      exit: develop-exit-de
      display_name: Netherlands via Germany
      service_credential_ref: secret://kv/develop/bridges/develop-entry-nl.to-develop-exit-de#service_uuid
```

`entries` и `exits` содержат ID логических нод, не instance ID. Одна LogicalNode
может состоять максимум в одном Fleet.

### Как расставлять связи entry → exit?

Каждая связь задаётся явно в `bridges`. Наличие entry и exit в одном Fleet само
по себе bridge не создаёт.

Для одной entry и двух exits нужны две записи:

```yaml
bridges:
  - routing_key: develop-entry-nl.to-develop-exit-de
    entry: develop-entry-nl
    exit: develop-exit-de
    display_name: Netherlands via Germany
    service_credential_ref: secret://kv/develop/bridges/develop-entry-nl.to-develop-exit-de#service_uuid
  - routing_key: develop-entry-nl.to-develop-exit-fr
    entry: develop-entry-nl
    exit: develop-exit-fr
    display_name: Netherlands via France
    service_credential_ref: secret://kv/develop/bridges/develop-entry-nl.to-develop-exit-fr#service_uuid
```

Правила bridge:

- направление всегда `entry → exit`;
- обе ноды состоят в том же Fleet;
- `routing_key` строго равен `<entry-id>.to-<exit-id>`;
- пара entry/exit уникальна внутри Fleet;
- credential принадлежит bridge и хранится в Vault;
- трафик bridge идёт на public address exit, не через management WireGuard;
- Xray tag выводится из LogicalNode exit: `xo-<exit-node-id>`, поэтому при
  замене instance правила клиентов не меняются.

### Шаг 6. Создать VPS и подготовить SSH

Provider adapter сейчас manual: репозиторий проверяет декларацию, но не создаёт
и не удаляет VPS. Создайте машину у провайдера и установите public half
environment-specific Ansible SSH key в `/root/.ssh/authorized_keys`.

Создайте отдельную пару для каждой среды, если её ещё нет:

```bash
umask 077
ssh-keygen -t ed25519 -a 100 -N '' \
  -f ~/.config/spiritvpn/keys/fleet-develop \
  -C spiritvpn-fleet-develop
```

Его private half позже хранится в Vault по фиксированной ссылке:

```text
secret://kv/develop/executor/ansible#private_key
```

Независимо получите SSH host key каждого VPS. На management host файл
`/etc/spiritvpn/deploy/develop/known_hosts` должен содержать host key как для
public bootstrap address, так и для вычисленного management address. Не
создавайте trust через `ssh-keyscan` в deployment job.

### Шаг 7. Заполнить Vault references

После добавления объектов получите полный список:

```bash
python3 scripts/vault-secret-resolver.py \
  --root . \
  --environment develop \
  --list-references
```

Для примера потребуются:

```text
secret://kv/develop/executor/ansible#private_key
secret://kv/develop/nodes/develop-entry-nl/reality#private_key
secret://kv/develop/nodes/develop-entry-nl/mask#fullchain
secret://kv/develop/nodes/develop-entry-nl/mask#private_key
secret://kv/develop/nodes/develop-exit-de/reality#private_key
secret://kv/develop/nodes/develop-exit-de/mask#fullchain
secret://kv/develop/nodes/develop-exit-de/mask#private_key
secret://kv/develop/bridges/develop-entry-nl.to-develop-exit-de#service_uuid
```

Каждое значение записывается интерактивно через
`spiritvpn-vault-operator put`, как показано в разделе про Vault.

### Шаг 8. Подготовить protected executor inputs

На management host для каждой среды нужны:

```text
/etc/spiritvpn/deploy/develop/readiness.yml
/etc/spiritvpn/deploy/develop/vault-approle/{role-id,secret-id,secret-id-accessor}
/etc/spiritvpn/deploy/develop/ca/develop/{ca.crt,ca.key}
```

`bootstrap.yml` в этот список не входит: platform reconciliation каждый раз
строит его из точного `inventories/bootstrap/platform.sops.yml` применяемого
Git SHA и локального public key management WireGuard. Ручная правка будет
перезаписана. Certificate chains новых агентов coordinator выпускает во
временный защищённый файл во время bootstrap и не сохраняет как desired state.

`known_hosts` также не является входным файлом executor: он компилируется из
SOPS topology вместе с inventory для точного Git SHA.

`ca/` — корень CA среды. Он нужен здесь потому, что приватный ключ агента
генерируется на ноде и никогда её не покидает: подписать CSR может только тот,
кто исполняет bootstrap, а исполняет его management host. Каталог
per-environment намеренно — исполнитель `develop` не должен читать корень
`prod`. Внутренний подкаталог `develop/` создаёт сам CA-адаптер, поэтому имя
среды в пути повторяется. Скопируйте туда `~/.config/spiritvpn/ca/<env>/` с
машины оператора как root-owned `0600`; без него apply с новыми нодами
останавливается до того, как тронет хоть одну машину.

`vault-approle/` создаётся командой:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator configure develop
```

`readiness.yml` оператор пока устанавливает как root-owned файл с mode `0600`.
Шаблон:

- [`examples/fleet-executor-readiness.yml`](examples/fleet-executor-readiness.yml).

[`examples/fleet-executor-bootstrap.yml`](examples/fleet-executor-bootstrap.yml)
остаётся только справочником формы автоматически создаваемого файла.

Пример передачи шаблонов после platform bootstrap:

```bash
scp \
  examples/fleet-executor-readiness.yml \
  deploy@MANAGEMENT_WIREGUARD_IP:/tmp/

ssh -t deploy@MANAGEMENT_WIREGUARD_IP \
  'sudo install -o root -g root -m 0600 \
    /tmp/fleet-executor-readiness.yml \
    /etc/spiritvpn/deploy/develop/readiness.yml'
```

Замените placeholder в `readiness.yml`. Других рукописных public/operator
входов fleet bootstrap не использует.

### Шаг 9. Проверить и отрендерить

После изменения:

```bash
make fleet-validate
make fleet-render ENVIRONMENT=develop
make fleet-ansible-check ENVIRONMENT=develop
make fleet-provisioning-check ENVIRONMENT=develop
make fleet-dns-plan ENVIRONMENT=develop \
  CLOUDFLARE_TOKEN_FILE=.local-secrets/cloudflare-token.txt
```

Посмотрите конкретные результаты, а не только exit code:

```bash
python3 -m json.tool build/develop/node-plans/develop-entry-nl-01.json
python3 -m json.tool build/develop/dns-plan.json
python3 -m json.tool build/develop/monitoring-targets.json
```

Результат рендера:

```text
build/<environment>/
├── ansible-inventory.json
├── bootstrap-inventory.json
├── dns-plan.json
├── monitoring-targets.json
├── node-plans/<instance-id>.json
└── impact-plan.json
```

Plan строится только из commit, поэтому сначала зафиксируйте изменения:

```bash
git add desired/
git commit -m "feat: define initial develop fleet"
make fleet-plan ENVIRONMENT=develop SOURCE=HEAD INITIAL=1
python3 -m json.tool build/develop/impact-plan.json
```

`INITIAL=1` нужен только при первом деплое среды, пока отсутствует
`refs/deployments/<environment>`. В обычном режиме baseline берётся из этого
deployment ref.

Dry-run координатора не подключается к нодам:

```bash
make fleet-deploy ENVIRONMENT=develop SOURCE=HEAD INITIAL=1
```

### Шаг 10. Первый bootstrap fleet-нод: текущая граница

При apply координатор:

1. собирает CSR со всех новых нод (`playbooks/bootstrap/csr.yml`);
2. подписывает их локальным CA и складывает цепочки рядом с deployment record;
3. подключается к новым VPS по public address и гоняет полный bootstrap
   (hardening, Docker, WireGuard, установка подписанной цепочки);
4. применяет Xray/runtime secrets;
5. выполняет direct и entry-to-exit smoke tests.

**Машинная идентичность — двухфазная по построению.** Приватный ключ агента
генерируется на ноде и никогда её не покидает, поэтому CA видит только CSR, и
чистая нода не бутстрапится одним проходом. Фаза CSR намеренно узкая: на ноде
работает только `roles/pki_agent`, и `any_errors_fatal` там снят — недоступная
нода не должна прятать запросы остальных. Установка цепочек падает fail-closed
на тех, чей CSR собрать не удалось.

Отсюда обязательный `CA_STATE` при apply с новыми нодами:

```bash
make fleet-deploy ENVIRONMENT=develop SOURCE=HEAD INITIAL=1 APPLY=1 \
  CA_STATE=~/.config/spiritvpn/ca \
  BOOTSTRAP_VARS=... COMPILED_SECRETS=... READINESS_VARS=...
```

Ручной путь (`make fleet-bootstrap`) остаётся односкачковым: там роль печатает
CSR в вывод, оператор подписывает его `make fleet-pki-sign` и повторяет прогон,
дополнив `spiritvpn_agent_certificate_chains`.

Не автоматизировано до сих пор:

- `readiness.yml` требует реальные `spiritvpn_direct_smoke_argv` и
  `spiritvpn_entry_exit_smoke_argv`;
- backend ApplyFleetManifest, автоматическая DNS promotion внутри coordinator и
  deployment ref advancement ещё не реализованы. DNS можно сверить и применить
  отдельной защищённой командой `fleet-dns-plan`/`fleet-dns-apply`.

Bootstrap останавливается fail-closed и может быть продолжен с тем же SHA через
`RESUME=1`; уже подписанные цепочки при этом не выпускаются заново.

### Как добавить ещё одну новую LogicalNode?

1. Создать новый `nodes/<id>.yml` с новой REALITY identity.
2. Создать её первый `instances/<id>-01.yml`.
3. Добавить node ID в `entries` или `exits` ровно одного Fleet.
4. При необходимости добавить явные bridge-записи.
5. Записать новые secret references в Vault.
6. Добавить host keys VPS в protected `known_hosts`.
7. Запустить validate → render → plan → dry-run.

### Как заменить VPS существующей LogicalNode?

Не создавайте новый node ID и не меняйте REALITY identity:

1. Создайте новый instance с новым неиспользованным slot и
   `target_state: candidate`.
2. Оставьте старый instance в `serving`.
3. Выполните bootstrap и synthetic проверки candidate.
4. После готовности переведите новый instance в `serving`, старый — в
   `draining` одним reviewed изменением.
5. После drain переведите старый в `retired` и только потом удалите VPS.

Автоматическое безопасное data-plane promotion этой последовательности пока не
завершено, поэтому ручное продвижение нельзя выполнять без внешней проверки
backend materialization и runtime health.

Схемы всех полей документированы в
[`contracts/desired-state/README.md`](contracts/desired-state/README.md). Полный
искусственный пример расположен в
[`tests/fixtures/valid/`](tests/fixtures/valid/).

## Как проходит обычный Git flow?

Изменения management-платформы:

```text
feature branch
  → pull request
  → CI
  → merge в main
  → platform-deploy check
  → GitHub Environment approval
  → platform-deploy apply
```

Изменения флота:

```text
desired/develop или desired/prod
  → pull request
  → CI и impact plan
  → merge в main
  → fleet-deploy dry-run
  → approval
  → fleet-deploy apply
```

Workflow принимает только полный 40-символьный SHA, достижимый из `main`, и
передаёт management executor точный Git bundle. GitHub не получает Vault
credentials, SOPS age identity или fleet private keys.

## Какие GitHub Environment secrets нужны?

В GitHub Environments `develop` и `prod` используются одинаковые имена, но
разные значения:

| Secret | Что содержит |
|---|---|
| `PLATFORM_SSH_PRIVATE_KEY` | Private SSH key, соответствующий environment-bound public key на management VPS |
| `PLATFORM_SSH_HOST` | Адрес management VPS, известный только GitHub Environment |
| `PLATFORM_SSH_KNOWN_HOSTS` | Заранее проверенная строка SSH host key management VPS |

Эти секреты дают только доступ к restricted forced command. Они не являются
root-доступом к VPS и не заменяют Vault.

Добавление через GitHub CLI:

```bash
gh secret set PLATFORM_SSH_PRIVATE_KEY --env develop < /protected/github-develop
gh secret set PLATFORM_SSH_HOST --env develop
gh secret set PLATFORM_SSH_KNOWN_HOSTS --env develop < /protected/known-hosts
```

Для `prod` команды повторяются с `--env prod` и отдельным private key.

## Что делают Makefile-команды?

### Безопасные локальные команды

| Команда | Назначение |
|---|---|
| `make help` | Показать доступные цели |
| `make check` | Валидация, unit-тесты и syntax-check |
| `make lint` | YAML и Ansible lint активного контура |
| `make fleet-validate` | Проверить desired state всех сред |
| `make fleet-test` | Запустить unit-тесты |
| `make fleet-render ENVIRONMENT=develop` | Скомпилировать артефакты среды |
| `make fleet-plan ENVIRONMENT=develop SOURCE=HEAD` | Рассчитать impact относительно deployment ref |
| `make fleet-ansible-check ENVIRONMENT=develop` | Проверить generated inventory и node plans |
| `make fleet-provisioning-check ENVIRONMENT=develop` | Проверить ручные декларации VPS без provider API |
| `make fleet-dns-plan ENVIRONMENT=develop CLOUDFLARE_TOKEN_FILE=...` | Сверить serving entry/exit записи с Cloudflare без изменений |
| `make fleet-platform-check` | Проверить и временно расшифровать SOPS bootstrap bundle |

### Команды с сетью или изменениями

| Команда | Поведение |
|---|---|
| `make fleet-platform-bootstrap-check CONNECT=1` | Только SSH/syntax preflight management VPS |
| `make fleet-platform-bootstrap APPLY=1` | Реальный низкоуровневый bootstrap management VPS |
| `make fleet-platform-refresh APPLY=1` | Довезти изменённые значения бандла на уже захардененный хаб по существующему туннелю |
| `make fleet-bootstrap-check CONNECT=1` | Проверить SSH к новым fleet-нодам |
| `make fleet-bootstrap APPLY=1 BOOTSTRAP_VARS=...` | Установить чистые fleet-ноды |
| `make fleet-configure-check CONNECT=1` | Ansible check по compiled inventory |
| `make fleet-configure APPLY=1 COMPILED_SECRETS=...` | Применить compiled node plans |
| `make fleet-dns-apply APPLY=1 ENVIRONMENT=develop CLOUDFLARE_TOKEN_FILE=...` | Создать или обновить DNS-only записи serving entry/exit в Cloudflare; записи не удаляет |
| `make fleet-deploy ...` | Координатор; без `APPLY=1` всегда dry-run. С новыми нодами требует `CA_STATE` — иначе их CSR некому подписать |

## Что делают скрипты?

| Скрипт | Назначение | Когда запускать вручную |
|---|---|---|
| [`platform-bootstrap.sh`](scripts/platform-bootstrap.sh) | Единый guarded entrypoint: тесты → lint → SOPS → SSH → bootstrap → post-check | Основной путь первого bootstrap |
| [`bootstrap-platform.py`](scripts/bootstrap-platform.py) | Двухфазный bootstrap WireGuard и management-платформы; с `--reuse-tunnel` — только вторая фаза, по уже поднятому туннелю | Обычно вызывается Makefile, вручную только при разработке |
| [`platform-sops.py`](scripts/platform-sops.py) | Временная расшифровка и строгая валидация bootstrap bundle | Обычно через `make fleet-platform-check` |
| [`platform-bootstrap-check.py`](scripts/platform-bootstrap-check.py) | Проверка односерверного inventory и pinned known_hosts | Внутренняя часть SOPS/preflight-контура |
| [`platform-remote.sh`](scripts/platform-remote.sh) | Строго ограниченный SSH-клиент для GitHub workflows | Обычно только из Actions |
| [`bootstrap-self-hosted-runner.sh`](scripts/bootstrap-self-hosted-runner.sh) | Идемпотентная установка GitHub Actions runner на отдельный VPS | При создании или восстановлении runner |
| [`vault-secret-resolver.py`](scripts/vault-secret-resolver.py) | Поиск `secret://` ссылок и временное разрешение значений на management executor | `--list-references` можно использовать локально; разрешение выполняет executor |
| [`vendor-backend-contract.sh`](scripts/vendor-backend-contract.sh) | Обновление vendored backend contract из точного commit соседнего репозитория | Только при согласованном обновлении контракта |

Ни один bootstrap-скрипт не должен автоматически выполнять Vault init или
сохранять recovery material.

## Что делают GitHub workflows?

| Workflow | Назначение |
|---|---|
| `ci.yml` | Локально воспроизводимые тесты, render, inventory parse и lint без секретов |
| `platform-readiness.yml` | Read-only проверка management-платформы |
| `platform-deploy.yml` | Check/apply management state из точного SHA |
| `control-deploy.yml` | Check/apply PostgreSQL, migrations и backend выбранной среды |
| `fleet-deploy.yml` | Dry-run/apply desired state выбранной среды |

Deploy workflows используют GitHub Environments и self-hosted runner с label
`spiritvpn-deploy`. Один общий persistent runner для `develop` и `prod` —
временный компромисс; в дальнейшем среды лучше разделить или использовать
ephemeral runners.

### Как выкатывается новая версия backend?

Backend repository публикует два образа из одного commit: `spiritvpnd` и
`migrate`. После публикации в infrastructure PR меняются `source_git_sha` и два
digest в `Environment.spec.control.backend_release`. После merge запускается
`control-deploy` сначала в режиме `check`, затем `apply`.

Management executor сам получает control-секреты из Vault. Он не требует SSH к
отдельному backend host, потому что stack локален management VPS. Apply:

1. проверяет точный Git bundle и рендерит `control-plan.json`;
2. сравнивает Git-owned backup policy с явно одобренным локальным contract;
3. поднимает pinned PostgreSQL;
4. при изменении release создаёт pre-migration dump (для `prod` дополнительно
   требует настроенный внешний backup adapter);
5. запускает pinned migration image;
6. приводит restricted runtime DB role и backend к desired state;
7. ждёт `/health/ready` и только после этого записывает successful release.

Adapter argv принадлежит SOPS topology и попадает в Ansible только через
compiled `control-plan.json`. Локальный `control.yml` больше не является
источником значения: перед apply он служит approval marker и обязан точно
совпасть с Git. Check не требует marker и ничего не изменяет.

Повторный apply того же release не запускает миграции заново и не использует
`--force-recreate`. Неявная смена major version существующего PostgreSQL
отклоняется: такой upgrade требует отдельного backup/restore runbook.

### Как выкатывается новая версия NodeAgent?

Agent repository публикует multi-architecture image. В infrastructure PR
меняется `desired/common/components.yml:components.node_agent.digest` (или
environment/node override). Planner отмечает только затронутые instance, а
`fleet-deploy` применяет к ним digest-pinned service с persistent SQLite и
node-local mTLS. Readiness требует running container, HTTP `/health/ready` и
gRPC listener. При совпадающем digest и конфигурации контейнер не пересоздаётся.

Пока `desired/` не содержит реальных нод и digests, эти workflows корректно
не смогут выполнить live rollout.

## FAQ

### Можно ли запустить bootstrap до pull request?

Да. `scripts/platform-bootstrap.sh --apply` требует чистый committed checkout,
но не требует, чтобы commit уже находился в `main`. Это удобно для первого
проверочного развёртывания. После создания штатного контура обычные изменения
должны проходить через pull request и точный SHA из `main`.

### Что такое `PLATFORM_SSH_PRIVATE_KEY`?

Это private key, которым GitHub workflow входит на management VPS под
ограниченным пользователем `github-deploy`. Соответствующий public key привязан
к конкретной среде и forced command. Это не SSH host key и не операторский
root-ключ.

### Что такое `PLATFORM_SSH_HOST`?

Это адрес management VPS для GitHub workflow. Он хранится в GitHub Environment,
чтобы публичный management IP не появлялся в открытом репозитории.

### Что такое `PLATFORM_SSH_KNOWN_HOSTS`?

Это заранее проверенный SSH host public key вместе с адресом. Он защищает от
подмены management VPS. Получать его через `ssh-keyscan` прямо во время деплоя
нельзя: это обнаруживает ключ, но не подтверждает доверие к нему.

### Откуда берутся private keys?

- операторский SSH key создаётся оператором и хранится на его устройстве;
- GitHub SSH key создаётся отдельно для каждой среды, private half хранится в
  GitHub Environment;
- operator WireGuard private key создаётся на устройстве оператора;
- private key management WireGuard создаётся непосредственно на management VPS;
- private keys fleet WireGuard и PKI создаются непосредственно на fleet-нодах;
- Vault transport private key создаётся на management VPS.

В Git попадают только public keys либо ссылки на секреты.

### Зачем одновременно SOPS, GitHub Secrets и Vault?

У них разные задачи:

- SOPS защищает редкий bootstrap-вход, который должен версионироваться в Git;
- GitHub Environment secrets дают workflow минимальный restricted SSH-доступ;
- Vault хранит runtime-секреты приложений и fleet executor credentials.

Vault не может хранить ключи, необходимые для установки самого Vault, а GitHub
не должен получать все runtime-секреты.

### Скрыт ли сейчас весь состав флота и его public IP?

Нет, пока скрыт только bootstrap inventory management-платформы. Реальные среды
в `desired/` ещё пусты, а текущий compiler ожидает обычные YAML-файлы. Если
заполнить их реальными нодами сейчас, состав флота и `public_address` попадут в
Git plaintext.

До заполнения реальных флотов нужно выбрать и реализовать один из вариантов:

1. SOPS-encrypted desired state с временной расшифровкой перед validation и
   compilation;
2. хранение чувствительной topology во внешнем inventory/provider adapter;
3. осознанное хранение несекретной topology в приватном Git-репозитории.

Private keys и значения приложений в `desired/` запрещены в любом случае.

### Почему inventory флота не пишется вручную?

Ручной inventory быстро расходится с desired state. `fleetctl` генерирует один
inventory и отдельный node plan для каждой instance из проверенного commit.
Ручным остаётся только однохостовый bootstrap inventory management VPS, потому
что до запуска control plane его ещё некому сгенерировать.

### Будет ли каждая нода передеплоена при любом изменении?

Нет. Planner вычисляет `affected` instance. Ansible получает `--limit` только
для затронутых машин, а роли приводят их к нужному состоянию без
`--force-recreate`. Если desired state не изменился, Ansible не вызывается.

Это не заменяет постоянное обнаружение ручного drift: reconciliation loop для
неизменившихся нод пока не реализован.

### Почему Vault не инициализируется bootstrap-скриптом?

`vault operator init` однократно создаёт unseal shares и initial root token.
Автоматический запуск легко оставил бы их в terminal log, CI artifact или файле.
Поэтому bootstrap никогда не меняет initialization/seal state Vault. На новом
хосте recovery ceremony проводится людьми через отдельную интерактивную
команду; повторные bootstrap сохраняют уже инициализированный Vault.

### Что произойдёт при повторном bootstrap?

Сценарий повторно проверит входные данные и приведёт систему к тому же
состоянию. Существующие machine private keys не регенерируются, совпадающие
файлы не перезаписываются, а контейнеры не пересоздаются без необходимости.
Неуправляемый локальный WireGuard config автоматически не заменяется.

### Что уже не завершено?

- workflows `platform-readiness` и `platform-deploy` ещё не проверены
  end-to-end из `main`;
- новый `control-deploy` и NodeAgent runtime ещё не проверены на live-машинах;
- Vault ещё нужно наполнить runtime-секретами; initial root token следует
  отозвать после настройки ограниченного operator-доступа;
- реальные `develop` и `prod` topology ещё не описаны;
- шифрование полного fleet desired state ещё не реализовано;
- coordinator останавливается на `WAITING_FOR_BACKEND`;
- backend ApplyFleetManifest, DNS promotion и advancement deployment ref не
  подключены;
- центральный monitoring/alerts adapter не реализован.

### Почему coordinator останавливается на `WAITING_FOR_BACKEND`?

Инфраструктурные стадии bootstrap/configure/readiness реализованы, но адаптер
применения manifest в backend, проверка materialization, DNS promotion и
атомарное продвижение deployment ref ещё не связаны в один доказуемый workflow.
Координатор не сообщает ложный успех и останавливается на явной границе.

## Типовые проблемы

### `SOPS decryption failed`

Проверьте наличие правильной age identity вне репозитория и соответствие
recipient в `.sops.yaml`. Не расшифровывайте bundle в постоянный файл.

### `No inventory was parsed`

Активируйте `ansible-env`, задайте writable temporary directory и используйте
команды Makefile, которые передают явный test или generated inventory:

```bash
source ansible-env/bin/activate
export ANSIBLE_LOCAL_TEMP=/tmp/spiritvpn-ansible-local
make check
```

### SSH preflight возвращает `UNREACHABLE`

Проверьте доступность TCP/22, private key оператора, пользователя из SOPS
inventory и точное совпадение pinned host key. Не отключайте
`StrictHostKeyChecking`.

### WireGuard поднят, но handshake отсутствует

Проверьте provider firewall для UDP/51820, endpoint, системное время и
соответствие локального private key public peer в SOPS bundle:

```bash
sudo wg show spiritvpn-mgmt
```

### Self-hosted runner показывает `offline`

На runner VPS проверьте systemd service, исходящий HTTPS к GitHub и отсутствие
второго процесса с тем же runner registration. Bootstrap runner описан в
[SELF_HOSTED_RUNNER.md](docs/operations/SELF_HOSTED_RUNNER.md).

## Где читать подробнее?

- [Русский операторский гайд](docs/operations/INFRA_V1_GUIDE_RU.md)
- [Bootstrap management-платформы](docs/operations/PLATFORM_BOOTSTRAP.md)
- [Переход от bootstrap к Git flow](docs/operations/BOOTSTRAP_TO_GIT_FLOW_RU.md)
- [Установка self-hosted runner](docs/operations/SELF_HOSTED_RUNNER.md)
- [Описание desired state](desired/README.md)
- [Документация fleetctl](fleetctl/README.md)
- [Индекс документации](docs/README.md)
