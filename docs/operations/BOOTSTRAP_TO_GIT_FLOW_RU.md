# От первого запуска к обычному Git flow

У системы теперь две разные фазы. Их нельзя смешивать.

## 1. Одноразовый bootstrap

Оператор запускает его со своего компьютера, где хранится age identity для
SOPS. В Git находится только ciphertext
`inventories/bootstrap/platform.sops.yml`; GitHub runner этот ключ не получает.

Перед запуском на чистом management VPS нужен только SSH-доступ, независимо
проверенный SSH host key и установленный провайдером операторский SSH public
key. WireGuard устанавливает сам bootstrap. На ноутбуке заранее создаётся
WireGuard keypair; в SOPS попадает только public key:

```bash
umask 077
wg genkey > ~/.config/spiritvpn/keys/operator-wg.key
wg pubkey < ~/.config/spiritvpn/keys/operator-wg.key
```

Первый Ansible connection использует публичный адрес из SOPS inventory. IP
ноутбука нигде не описывается. После создания WireGuard прямой публичный SSH
остаётся только для runner, а операторы входят через management overlay.

Рекомендуемый единый сценарий сначала выполняет все проверки, затем требует
явного подтверждения и только после него изменяет management VPS:

```bash
scripts/platform-bootstrap.sh --apply
```

Без `--apply` он выполняет только безопасную проверку полного входного контура:

```bash
scripts/platform-bootstrap.sh
```

Низкоуровневые команды, которые сценарий выполняет последовательно:

```bash
make fleet-platform-check
make fleet-platform-bootstrap-check CONNECT=1
make fleet-platform-bootstrap APPLY=1
```

`bootstrap-check` для чистой машины — это syntax/connectivity preflight, а не
симуляция создания ключей: Ansible check mode не может проверить файлы, которые
ещё не были сгенерированы.

Команда `fleet-platform-bootstrap` выполняет две фазы автоматически. Сначала
она устанавливает WireGuard на management при ещё открытом публичном SSH,
создаёт и поднимает на ноутбуке управляемый интерфейс `spiritvpn-mgmt`, затем
проверяет pinned SSH через tunnel. Только после успешной проверки запускаются
firewall, Docker, Vault и executor. В конце playbook применяется второй раз для
проверки сходимости. Скрипт запросит локальный `sudo`, но ручное редактирование
WireGuard-конфигурации не требуется.

Bootstrap устанавливает WireGuard на management, генерирует private key только
на VPS, настраивает hub addresses и operator peers, затем включает firewall.
При bootstrap fleet-ноды также генерируют private keys локально; их public keys
автоматически регистрируются на management hub до проверки туннеля.

Bootstrap устанавливает и настраивает:

- host hardening и nftables;
- Docker;
- незапущенный в эксплуатацию, но работающий процесс Vault;
- ограниченного пользователя `github-deploy`;
- локальные исполнители `platform-readiness`, `platform-deploy` и
  `fleet-deploy`.

После этого оператор вручную выполняет ceremony Vault: initialize, разносит
unseal shares во внешнее recovery-хранилище, unseal, создаёт environment policy
и AppRole, затем загружает начальные секреты. Эти данные не попадают ни в Git,
ни в GitHub Secrets.

## 2. Обычные изменения после bootstrap

Поток для кода management-компонентов:

```text
branch -> pull request -> checks -> merge в main
       -> platform-deploy(check) -> approval -> platform-deploy(apply)
```

В workflow указывается полный SHA merge-коммита. Workflow проверяет, что SHA
достижим из `main`, и передаёт на management только точный Git bundle. Root-
исполнитель принимает только разрешённую команду, проверяет bundle и запускает
локальный `playbooks/platform/steady.yml`. Переменные доступа берутся из файла
`/etc/spiritvpn/platform/runtime-vars.yml`, созданного bootstrap с mode `0600`.

Изменение зашифрованного bootstrap bundle — редкое исключение. Оно проходит PR,
но применяется оператором повторным `fleet-platform-bootstrap`, потому что
меняет SSH/host-key/access boundary. Обычный `platform-deploy` этот ciphertext
намеренно не расшифровывает.

Поток для двух флотов независим:

```text
desired/develop -> PR -> merge -> fleet-deploy(develop, dry-run) -> apply
desired/prod    -> PR -> merge -> fleet-deploy(prod, dry-run)    -> approval -> apply
```

Обе среды пока используют один выделенный self-hosted runner и один management
VPS, но разные GitHub Environment secrets и разные forced-command SSH keys.

`fleetctl` вычисляет `impact-plan`. Bootstrap запускается только для новых
инстансов; configure и readiness — только для `affected` инстансов. Если desired
state не изменился, Ansible вообще не вызывается. Если Ansible вызван для
затронутой ноды, роли работают как reconciliation: одинаковые файлы не
перезаписываются, а `docker compose up` не пересоздаёт совпадающие контейнеры.
Успешно завершённый этап при `resume` повторно не запускается.

## 3. Что ещё не завершено

- Vault нужно реально bootstrap/init/unseal и проверить восстановление snapshot.
- Нужно установить root-owned executor inputs и наполнить Vault секретами для
  `develop`, затем отдельно для `prod`.
- Coordinator пока останавливается на границе backend; полноценное применение
  backend/DNS и продвижение deployment ref ещё не завершено.
- Центральный monitoring/alerts adapter ещё не реализован. Node exporter и
  monitoring targets сами по себе не создают центральный стек.
- Общий persistent runner — временный компромисс: позже среды лучше разделить
  или перейти на ephemeral runners.
