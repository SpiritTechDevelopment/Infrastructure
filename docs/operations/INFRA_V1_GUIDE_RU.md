# Руководство оператора SpiritVPN Infrastructure v1

Этот документ описывает эксплуатацию текущего `main`: подготовку рабочего
места, изменение желаемого состояния, работу CI и процессов развёртывания,
первичную настройку управляющей платформы, Vault, выделенный раннер,
развёртывание флота и разбор
типовых отказов.

Общая архитектура кратко изложена в [корневом README](../../README.md).
Остальные документы в `docs/` пока являются справочными и могут содержать
историческое состояние реализации. При расхождении ориентируйтесь на этот
руководство и исполняемый код текущего коммита.

## 1. Неподвижные правила

1. Git описывает желаемое состояние; серверы его исполняют.
2. Обнаруженное состояние сервера не импортируется в Git автоматически.
3. Runtime-секреты сред хранятся в Vault, а Git содержит ссылки `secret://`.
4. Адреса, домены, порты, сертификаты и чувствительные входные данные платформы
   попадают в Git только внутри SOPS ciphertext.
5. Закрытые ключи машин остаются на машине, которая ими владеет.
6. Развёртывание всегда привязано к полному Git SHA.
7. `prod`, опасные изменения, первое развёртывание и изменение границы доступа
   требуют отдельного решения оператора.
8. Ручная правка сервера не становится желаемым состоянием. Если настройка
   должна переживать reconcile, ей нужен владелец в Git.
9. Аудит без изменений может читать фактическое состояние, но не должен его изменять.
10. Открытый текст, временные планы и разрешённые значения секретов нельзя коммитить, кэшировать
    или прикладывать к GitHub Actions.

## 2. Текущие контуры

### Управляющая платформа

Один управляющий сервер выполняет общие функции:

- управляющий WireGuard hub;
- Vault;
- ограниченные исполнители `github-deploy`;
- координатор развёртывания и его постоянное состояние;
- Prometheus, Alertmanager, Grafana, Loki, Alloy и host exporter;
- изолированные управляющие контуры сред.

Первичная настройка и контракт доступа находятся в
`inventories/bootstrap/platform.sops.yml`. Обычный `platform-deploy` применяет
постоянное состояние только после того, как чувствительная часть контракта
доступа явно принята защищённым обновлением.

### Среда `develop`

В SOPS topology объявлены:

- объект окружения с выпусками control и bot;
- один VPN-флот;
- одна входная нода;
- одна выходная нода;
- конкретные instances и общие переопределения компонентов.

Успешный коммит CI из `main` автоматически запускает сверку develop.
Флаг опасных изменений в автоматическом пути всегда выключен.

### Среда `prod`

Prod topology сейчас содержит только объект окружения. Автоматическое применение
отключено. Процесс выпуска создаёт pull request, а не отправляет выпуск prod
прямо в `main`. До наполнения topology и настройки отдельного процесса
подтверждения и учётных данных prod не считается рабочим контуром развёртывания.

## 3. Владельцы состояния

| Состояние | Канонический владелец |
|---|---|
| Топология среды | `desired/environments/<env>/topology.sops.yml` |
| Общие версии и настройки | `desired/common/*.yml`, SOPS |
| Идентификаторы флотов | `desired/fleet-ids.yml`, SOPS |
| Контракт управления и раннера | `inventories/bootstrap/platform.sops.yml` |
| Значения runtime-секретов | Vault |
| Схемы и логика применения | `contracts/`, `fleetctl/`, `roles/`, `playbooks/` |
| Автоматизация | `.github/workflows/` и `scripts/` |
| Применённая база флота | `refs/deployments/<environment>` |
| Состояние продолжения и ревизий | `/var/lib/spiritvpn/fleetctl` |
| Закрытые ключи серверов | Только соответствующий сервер |
| Явно применённый контракт доступа | `/etc/spiritvpn/platform/runtime-vars.yml`, производная запись |

`runtime-vars.yml`, сгенерированные inventory, планы нод и записи развёртывания
не являются альтернативным желаемым состоянием. Они нужны для fail-closed
проверки, продолжения и аудита результата.

## 4. Подготовка рабочего места

Нужны:

- Git и доступ к репозиторию;
- Python 3 и Make;
- SOPS и age identity оператора;
- Ansible Core 2.18;
- инструменты WireGuard для первичной настройки и обновления платформы;
- GitHub CLI только для операторских действий через API и диагностики Actions.

Создайте локальное окружение:

```bash
python3 -m venv ansible-env
ansible-env/bin/pip install -r requirements-ansible.txt
export PATH="$PWD/ansible-env/bin:$PATH"
export ANSIBLE_LOCAL_TEMP=/tmp/spiritvpn-ansible-local
```

Если age identity не находится SOPS автоматически, задайте путь только в
локальной shell-сессии:

```bash
export SOPS_AGE_KEY_FILE="$HOME/.config/spiritvpn/sops/age-identity.txt"
```

Не копируйте идентичность оператора на раннер и не используйте идентичность
раннера на ноутбуке. У раннера, управляющего исполнителя и владельца recovery
разные recipients.

### Как получить доступ

Ключи оператора не выдаются «сверху»: вы создаёте их сами, а действующий
оператор вносит в объявления только публичные части. Весь путь — создание
личности, сверка отпечатка, подключение к оверлею, внутренние имена и то, что
после этого доступно — описан в
[операторском гайде](../../dev/OPERATORS_GUIDE.md).

Здесь этот путь намеренно не пересказан: два документа, описывающие одну
процедуру, расходятся, и расходятся молча.

## 5. Начало операторской сессии

Перед изменением получите актуальное состояние remote:

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git status --short --branch
git rev-parse HEAD
```

Рабочее дерево должно быть чистым. Если в нём есть чужие или несвязанные
изменения, не удаляйте и не перезаписывайте их.

Для обычной работы используйте отдельную ветку и pull request. Если согласованный
процесс допускает локальный коммит поверх `main`, учитывайте, что бот выпуска
может одновременно продвинуть удалённую ветку. В таком случае для ещё не
опубликованных локальных коммитов:

```bash
git fetch origin
git rebase origin/main
git push origin main
```

Не запускайте rebase для уже опубликованной истории и не используйте force-push
в `main`.

## 6. Редактирование желаемого состояния

### Выбор файла

| Изменение | Файл |
|---|---|
| Окружение, control, bot, флот, ноды, instances | `desired/environments/<env>/topology.sops.yml` |
| Общие закреплённые образы | `desired/common/components.yml` |
| Общие сети | `desired/common/networking.yml` |
| Лимиты | `desired/common/limits.yml` |
| Политика наблюдаемости | `desired/common/observability.yml` |
| Политика выкладки | `desired/common/rollout.yml` |
| Xray defaults | `desired/common/xray.yml` |
| Стабильный идентификатор флота | `desired/fleet-ids.yml` |
| Управляющая платформа, доступ и раннер | `inventories/bootstrap/platform.sops.yml` |

Файлы `desired/common/*.yml` также зашифрованы SOPS, даже если в имени нет
суффикса `.sops`.

### Безопасное редактирование

Открывайте файл непосредственно через SOPS:

```bash
sops desired/environments/develop/topology.sops.yml
```

или:

```bash
sops inventories/bootstrap/platform.sops.yml
```

Запрещено:

- выполнять `sops -d FILE > tracked-file.yml`;
- использовать `sops --decrypt --in-place`;
- оставлять plaintext под `desired/`, `inventories/`, `build/` или в корне;
- вставлять фактическое состояние сервера только потому, что оно было обнаружено;
- добавлять значения Vault вместо ссылки `secret://`.

Новая конфигурация должна быть осознанным желаемым состоянием. Enrollment public
key можно внести только в рамках явной процедуры, после проверки логического
объекта и владельца закрытого ключа.

## 7. Локальные проверки

Минимум для любого изменения:

```bash
git diff --check
make ANSIBLE_VENV=./ansible-env check
make lint
```

Проверка platform bundle:

```bash
python3 scripts/platform-sops.py check \
  --bundle inventories/bootstrap/platform.sops.yml
```

Проверки конкретного environment:

```bash
python3 -m fleetctl.cli validate --environment develop
python3 -m fleetctl.cli render \
  --environment develop \
  --output build/develop
python3 -m fleetctl.cli ansible-check \
  --environment develop \
  --build-dir build/develop
```

План относительно применённого deployment ref:

```bash
make fleet-plan ENVIRONMENT=develop SOURCE=HEAD
```

Если deployment ref ещё не существует, используйте `INITIAL=1` только для
осознанного первого deployment:

```bash
make fleet-plan ENVIRONMENT=develop SOURCE=HEAD INITIAL=1
```

`build/` является временным результатом и не коммитится.

## 8. Коммит и отправка

Перед коммитом проверьте состав файлов:

```bash
git status --short
git diff --stat
git diff --check
```

Для SOPS diff показывает ciphertext. Смысл изменения проверяется через
валидатор и просмотр в `sops`, а не через публикацию decrypted diff.

После коммита:

```bash
git fetch origin
git rebase origin/main
git push origin HEAD
```

Rebase здесь допустим только для локальных неопубликованных коммитов. Если бот
выпуска снова успел продвинуть `main`, повторите `fetch → rebase → push`.

## 9. Что делает CI

### Запрос на слияние

GitHub-hosted runner не имеет age identity. Он проверяет:

- SOPS envelopes;
- schemas и compiler на synthetic fixture;
- unit-тесты;
- Ansible syntax и lint.

Он не может раскрыть live topology.

### Отправка в `main`

Дополнительно запускается trusted job на self-hosted runner. Он использует
локальную SOPS identity, расшифровывает реальный `develop` и `prod` только в
процессе, валидирует topology и проверяет детерминированность render.

После успешного CI запускается `desired-state-deploy`. Процесс:

1. checkout точного `workflow_run.head_sha`;
2. проверяет, что SHA всё ещё является текущим `main`;
3. определяет затронутые platform/control/fleet контуры;
4. применяет `develop` в порядке platform → control → fleet;
5. не пропускает `prod` в автоматический apply;
6. не разрешает initial или destructive fleet deployment.

Глобальное изменение кода, роли, common desired state или workflow считается
влияющим на все контуры. Это намеренно консервативное поведение.

## 10. Процессы развёртывания

| Процесс | Назначение | Режимы |
|---|---|---|
| `platform-deploy` | Постоянное состояние управления и наблюдаемость | `check`, `apply` |
| `control-deploy` | PostgreSQL, backend, bot и цели мониторинга | `check`, `apply` |
| `fleet-deploy` | Первичная настройка, конфигурация, готовность и манифест | `dry-run`, `apply` |
| `platform-readiness` | Проверка управляющей основы без изменений | без apply |
| `runner-sops-bootstrap` | Создание локальной age identity раннера | ручной запуск |

Каждый процесс развёртывания принимает полный `source_git_sha`, проверяет его
достижимость из `main`, создаёт Git bundle и вызывает только разрешённую forced
команду на управляющем сервере.

SSH material существует только во временном каталоге job и удаляется в
`always()` cleanup. Учётные данные Vault и закрытый SSH-ключ флота на раннер не
передаются.

## 11. Обычное изменение `develop`

Для безопасного изменения действующего develop оператор обычно не запускает
развёртывание вручную:

1. Изменить SOPS desired state или код.
2. Пройти проверку и CI.
3. Выполнить merge/push в `main`.
4. Дождаться `desired-state-deploy`.
5. Проверить последовательность platform → control → fleet.
6. Убедиться, что Git-ссылка развёртывания переместилась только после `BACKEND_APPLIED`.
7. Проверить готовность, оповещения, панели и логи.

Следующий зелёный коммит снова запускает сверку даже тогда, когда
предыдущая выкатка не дошла до конца. Старый вручную перезапущенный CI не может
откатить текущий `main`.

## 12. Выпуск backend, agent и bot

`release-bump` получает проверенный repository dispatch от репозитория
компонента. Payload содержит тип компонента, environment, source SHA и digest
неизменяемого образа.

Процесс:

1. валидирует payload;
2. использует локальную SOPS identity раннера;
3. меняет только соответствующий выпуск внутри topology в памяти;
4. проверяет, что изменён ровно один topology-файл;
5. валидирует environment;
6. для develop коммитит и пушит в `main`;
7. для prod создаёт ветку выпуска и pull request.

Если локальный push оператора получает `non-fast-forward` во время выпуска,
дождитесь завершения процесса бота, затем выполните:

```bash
git fetch origin
git rebase origin/main
git push origin main
```

Не останавливайте процесс выпуска и не делайте force-push ради выигрыша гонки.

## 13. Первичная настройка и защищённое обновление платформы

### Новый управляющий сервер

Первичная настройка разрешена только после независимой проверки SSH host key и
заполнения SOPS-контракта платформы.

Полная read-only проверка:

```bash
scripts/platform-bootstrap.sh
```

Первичная установка с интерактивным подтверждением:

```bash
scripts/platform-bootstrap.sh --apply
```

Скрипт сначала создаёт управляющий WireGuard, затем проверяет туннель и только
после этого включает усиленный firewall, Vault, исполнителей и наблюдаемость. Он
отказывается перезаписывать неуправляемую локальную конфигурацию WireGuard.

### Изменение действующего runtime-контракта платформы

После hardening публичный SSH первичной настройки больше не является штатным
путём. Если меняется runtime/access projection platform bundle:

1. Отредактировать SOPS bundle.
2. Проверить его.
3. Провести проверку и создать коммит.
4. Из чистого checkout точного коммита выполнить защищённое обновление через overlay.
5. После успешного обновления отправить либо перезапустить обычное развёртывание.

Команда:

```bash
scripts/platform-bootstrap.sh --reuse-tunnel --apply
```

Низкоуровневый эквивалент:

```bash
make fleet-platform-refresh \
  APPLY=1 \
  PLATFORM_BUNDLE=inventories/bootstrap/platform.sops.yml \
  PLATFORM_WIREGUARD_PRIVATE_KEY="$HOME/.config/spiritvpn/keys/operator-wg.key"
```

Ожидаемый fail-closed текст до обновления:

```text
reviewed platform bundle differs from the explicitly applied access contract
```

Не обходите эту проверку и не редактируйте
`/etc/spiritvpn/platform/runtime-vars.yml` вручную. После защищённого обновления
файл перезаписывается из проверенного SOPS-контракта, а обычный
`platform-deploy` сверяет постоянное состояние.

Изменение только плана хоста раннера не входит в runtime projection управляющей
платформы. Его нужно применить отдельной процедурой раннера, а не обновлением
платформы.

### Выдача и отзыв операторского доступа

Обе процедуры описаны в [операторском гайде](../../dev/OPERATORS_GUIDE.md):
`make operator-grant` и `make operator-revoke` правят только объявления и
останавливаются, оставляя diff.

К этому разделу они относятся последним шагом: контракт доступа не проходит
через обычный `platform-deploy`, поэтому применяется тем же защищённым
обновлением, что описано выше:

```bash
scripts/platform-bootstrap.sh --reuse-tunnel --apply
```

Пока это не выполнено, выдача не действует, а отзыв не состоялся: старый ключ и
peer продолжают работать.

## 14. Vault

Первичная настройка платформы устанавливает Vault, но никогда автоматически не
выполняет init, unseal и запись значений секретов.

Все команды запускаются на управляющем сервере через root-owned wrapper:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator status
sudo /usr/local/sbin/spiritvpn-vault-operator init 5 3
sudo /usr/local/sbin/spiritvpn-vault-operator unseal
sudo /usr/local/sbin/spiritvpn-vault-operator configure develop
sudo /usr/local/sbin/spiritvpn-vault-operator snapshot
```

Параметры `init 5 3` являются примером. Реальную share policy нужно утвердить
до инициализации. Init response выводится один раз и должен сразу попасть во
внешнее recovery storage. Unseal shares и initial root token нельзя сохранять
на VPS, в Git, shell history или GitHub.

Получить список ссылок, необходимых текущему desired state:

```bash
python3 scripts/vault-secret-resolver.py \
  --root . \
  --environment develop \
  --list-references
```

Записать значение интерактивно на management host:

```bash
sudo /usr/local/sbin/spiritvpn-vault-operator put develop \
  secret://kv/develop/PATH#FIELD
```

Значение читается со стандартного ввода и не должно попадать в аргументы команды.
После изменения значения при неизменной Git-ссылке автоматическое развёртывание не
запустится, поэтому нужный `control-deploy` или `fleet-deploy` следует вызвать
вручную для точного текущего SHA.

Входные данные первичной настройки и доступа сейчас принадлежат SOPS-контракту
платформы. Не переносите их в открытый текст. Миграция отдельного platform secret в
Vault должна быть самостоятельным изменением схемы и роли, а не ручным
перемещением значения на сервере.

## 15. Выделенный раннер

Раннер является отдельным сервером. Он не должен иметь учётные данные Vault,
закрытый SSH-ключ флота, членство в Docker или sudo у аккаунта `github-runner`.

### Проверка плана хоста из Git

Из чистого checkout точного SHA:

```bash
umask 077
python3 scripts/platform-sops.py runner-host-plan \
  --bundle inventories/bootstrap/platform.sops.yml \
  --source-git-sha "$(git rev-parse HEAD)" \
  --output /tmp/spiritvpn-runner-host-plan.json
```

Передайте план и соответствующий скрипт на раннер по операторскому каналу:

```bash
scp \
  scripts/bootstrap-self-hosted-runner.sh \
  /tmp/spiritvpn-runner-host-plan.json \
  RUNNER_ALIAS:/tmp/
```

Проверка без изменений на раннере:

```bash
sudo /tmp/bootstrap-self-hosted-runner.sh \
  --plan /tmp/spiritvpn-runner-host-plan.json \
  --mode check
```

`--mode apply` нужен только при явном создании или согласованном изменении
сервера. Registration token подаётся через стандартный ввод и не хранится в плане.

### SOPS-идентичность раннера

Процесс `runner-sops-bootstrap` создаёт identity идемпотентно и печатает
только public recipient. Закрытая identity должна остаться по локальному пути раннера
с владельцем `github-runner` и mode `0600`.

После добавления recipient в `.sops.yaml` SOPS-файлы нужно переобернуть обычной
SOPS-процедурой. Закрытая identity не копируется в GitHub secret.

### Проверка регистрации в управляющей сети

Сгенерируйте план из финального коммита:

```bash
umask 077
python3 scripts/platform-sops.py runner-plan \
  --bundle inventories/bootstrap/platform.sops.yml \
  --runner-id ci-runner \
  --source-git-sha "$(git rev-parse HEAD)" \
  --output /tmp/spiritvpn-runner-plan.yml
```

Передайте план и скрипт на раннер и выполните:

```bash
sudo /tmp/enroll-runner-overlay.sh \
  --plan /tmp/spiritvpn-runner-plan.yml \
  --mode check
```

Проверка должна подтвердить точное семантическое совпадение, активный интерфейс
и сервис. Закрытый ключ WireGuard никогда не копируется в Git или на hub. В
SOPS хранится только проверенный public key и желаемая overlay-конфигурация.

После процедуры удалите временные планы с обеих машин:

```bash
rm -f /tmp/spiritvpn-runner-host-plan.json /tmp/spiritvpn-runner-plan.yml
```

## 16. Первое развёртывание флота

Автоматический путь всегда передаёт `initial=false`. Поэтому первое
развёртывание нового environment выполняется вручную.

Сначала запустите `fleet-deploy` через GitHub Actions со значениями:

```text
environment=develop
source_git_sha=<полный SHA текущего main>
mode=dry-run
initial=true
resume=false
allow_destructive=false
```

Проверьте план влияния и запись развёртывания. Затем для того же SHA:

```text
environment=develop
source_git_sha=<тот же SHA>
mode=apply
initial=true
resume=true
allow_destructive=false
```

`resume=true` нужен потому, что dry-run уже создал запись и закрепил ревизию
манифеста. Продолжение другого SHA запрещено.

При применении координатор выполняет:

```text
validate
→ deployment baseline
→ impact plan
→ provisioning preflight
→ manifest revision
→ bootstrap CSR и certificate issuance для новых nodes
→ bootstrap
→ configure
→ readiness
→ ApplyFleetManifest
→ BACKEND_APPLIED
```

После `BACKEND_APPLIED` отдельная задача GitHub без SSH-доступа выполняет
compare-and-swap `refs/deployments/<environment>`. Сам управляющий исполнитель не
имеет права писать в репозиторий.

## 17. Повтор, продолжение и опасные изменения

### Когда нужен resume

Используйте `resume=true`, только если coordinator уже создал record для той же
пары environment/SHA и предыдущий запуск не завершился.

Если workflow упал до передачи bundle на management host, record мог не
появиться; тогда нужен обычный повтор с `resume=false`.

### Опасный план

Удаление членства во флоте, ноды, instance или bridge требует:

- отдельной проверки плана влияния;
- ручного запуска процесса;
- `allow_destructive=true`;
- плана вывода из эксплуатации и подтверждения внешней доступности.

Автоматическое развёртывание желаемого состояния никогда не включает этот флаг.
Удаление VPS у провайдера также остаётся отдельной ручной операцией.

### Ручное продвижение Git-ссылки развёртывания

Обычно ссылку двигает workflow. Ручная команда допустима только когда
развёртывание уже подтверждено отдельно, а автоматическая задача продвижения не выполнилась:

```bash
make fleet-promote \
  ENVIRONMENT=develop \
  SOURCE_GIT_SHA="применённый-SHA" \
  BASELINE_GIT_SHA="ожидаемый-прежний-SHA" \
  APPLY=1
```

Для первого развёртывания вместо baseline используется `INITIAL=1`. Нельзя
подставлять текущую ссылку после факта: compare-and-swap должен проверять именно
базу, относительно которой строилось развёртывание.

## 18. DNS

Адаптер DNS компилирует только записи из желаемого состояния, сверяет их с Cloudflare
и по умолчанию ничего не меняет.

План:

```bash
make fleet-dns-plan \
  ENVIRONMENT=develop \
  CLOUDFLARE_TOKEN_FILE=/protected/cloudflare-token
```

Применение создания и обновления:

```bash
make fleet-dns-apply \
  ENVIRONMENT=develop \
  CLOUDFLARE_TOKEN_FILE=/protected/cloudflare-token \
  APPLY=1
```

Файл токена должен быть обычным файлом mode `0600`. Адаптер не удаляет записи
DNS. Удаление и data-plane promotion остаются отдельной опасной процедурой.
Git-ссылка развёртывания флота может быть продвинута до отдельного применения DNS;
оператор обязан учитывать эту границу.

## 19. Проверки готовности, метрики и логи

`platform-readiness` выполняет проверку управляющей основы без изменений через
forced command. Проверка готовности флота запускается внутри `fleet-deploy`
после configure.

Проверка готовности флота проверяет, среди прочего:

- установленный exact node plan;
- management interface и address;
- systemd units;
- Xray syntax, container и API;
- NodeAgent process, listener и certificate identity;
- CAKE;
- node exporter;
- declarative smoke adapters.

Развёртывание платформы владеет общим стеком
Prometheus/Alertmanager/Grafana/Loki/Alloy. Развёртывание control публикует
ограниченные окружением цели сбора из скомпилированного
`monitoring-targets.json`. Ноды флота отправляют логи и предоставляют метрики
только через объявленные управляющие пути.

Управляющие интерфейсы наблюдаемости не следует открывать наружу. Prometheus,
Alertmanager, Loki и host exporter слушают только петлю управляющего сервера:
входящего вызывающего у них нет, сбор идёт исходящими запросами. Оператор
достаёт их локальным SSH-туннелем с портами из применённого контракта, без
копирования адресов в документацию.

Grafana — исключение: у неё единственной здесь человеческий интерфейс, и она
слушает адрес управляющего оверлея. Порядок подключения — в разделе
[«Подключение к управляющему оверлею»](#подключение-к-управляющему-оверлею).

Анонимный доступ в Grafana выключен, и это условие такой привязки, а не
предпочтение. На управляющем сервере оверлейный интерфейс доверен firewall'ом
целиком — правило по интерфейсу срабатывает раньше любого ограничивающего, —
поэтому слушатель на оверлейном адресе достижим для каждой ноды флота, а не
только для операторов. Дашборды показывают состав флота и объёмы трафика, и
единственное, что отделяет их от узла, вышедшего в оверлей, — требование войти.
Роль отказывается разворачивать связку «оверлейный адрес и анонимный доступ».

## 20. Диагностика отказов

Последний лог развёртывания:

```bash
make fleet-deploy-log WORKFLOW=fleet-deploy BACK=1
```

`WORKFLOW` можно заменить на нужное имя процесса.

| Сообщение | Причина | Действие |
|---|---|---|
| `reviewed platform bundle differs...` | Новый access contract ещё не принят | Выполнить guarded platform refresh |
| `source SHA ... current main` | Workflow относится к устаревшему commit | Не применять; дождаться CI текущего `main` |
| `deployment baseline is missing` | Первый deployment | Ручной dispatch с `initial=true` |
| `deployment record already exists` | Повтор той же environment/SHA | Убедиться в record и использовать `resume=true` |
| `resume record belongs to another source commit` | Resume запущен для другого SHA | Использовать исходный SHA либо начать новый deployment |
| `Vault ... sealed` | Vault перезапущен или не unsealed | Выполнить threshold unseal ceremony |
| `secret ... missing` | Vault reference не заполнена | Записать значение через operator wrapper |
| `SSH host key ...` | Pinned trust не совпал | Проверить host identity независимым каналом; не использовать `ssh-keyscan` |
| `fetch first` / `non-fast-forward` | Release bot или другой commit продвинул `main` | `git fetch`, rebase неопубликованного commit, повторный push |
| `runner WireGuard address is invalid` | Placeholder или неверный CIDR в SOPS | Исправить reviewed `/32`; не импортировать неизвестный runtime автоматически |

При отказе сначала определите границу: CI, runner, forced command, platform
contract, Vault resolution, Ansible, readiness, backend или promote. Не
исправляйте сервер вручную до определения Git-владельца настройки.

## 21. Аварийное расследование управляющего сервера

Этот runbook применяется, когда управляющий сервер перестал отвечать, пропали
метрики, PostgreSQL выполнил recovery, выросли CPU или память либо обнаружен
неизвестный процесс. Сначала собираются факты без изменений. Остановка,
изоляция, удаление, ротация и восстановление выполняются только после отдельного
решения оператора.

Команды выполняются непосредственно на управляющем сервере от действующей
операторской SSH-сессии. Вывод может содержать адреса, имена, параметры
сертификатов и другие операционные данные. Их нельзя публиковать в issue,
GitHub Actions, Git или незащищённом канале.

### 21.1. Порядок расследования

Соблюдайте порядок:

1. Зафиксировать время, uptime и давление на ресурсы.
2. Проверить control-контейнеры и PostgreSQL без перезапуска.
3. Проверить OOM, диск, ядро и историю загрузок.
4. Отделить нагрузку контейнеров от host-level cgroup.
5. Проверить неизвестный systemd unit, бинарник и его соединения.
6. Проверить фактическую SSH-политику и историю методов входа.
7. Составить вывод о причине, не изменяя сервер.
8. При подтверждённой компрометации сохранить доказательства и только затем
   выполнить согласованное сдерживание.

До окончания read-only этапа нельзя выполнять `reboot`, `docker restart`,
`systemctl restart`, `kill`, удаление файлов, очистку журналов, обновление
пакетов или ручную правку firewall. Эти действия уничтожают часть временной
шкалы и могут активировать дополнительный механизм persistence.

### 21.2. Первичный снимок хоста

```bash
date -Is
uptime
free -h
swapon --show
cat /proc/pressure/memory
vmstat 1 10
```

Важные поля:

| Поле | Значение |
|---|---|
| `MemAvailable` в `free` | Реальный запас памяти с учётом освобождаемого cache |
| `some` и `full` в PSI | Частичное и полное ожидание памяти |
| `r` в `vmstat` | Число готовых к выполнению процессов |
| `us` | CPU пользовательских процессов |
| `sy` | CPU ядра |
| `wa` | Ожидание I/O |
| `st` | CPU, отнятый гипервизором |
| `id` | Простой CPU |

Высокий процент `used` сам по себе не доказывает аварию: Linux использует
свободную память под cache. Признаками реального давления являются низкий
`available`, рост PSI, OOM и активный swap. На сервере без swap небольшой запас
`available` является эксплуатационным риском даже при нулевом PSI.

Снимок процессов и контейнеров:

```bash
ps -eo pid,ppid,user,comm,%cpu,%mem,rss,vsz,etimes --sort=-rss | head -n 25

sudo docker stats --no-stream \
  --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}\t{{.PIDs}}'
```

`ps %CPU` усредняет CPU за время жизни процесса и может не показать короткий
всплеск. `docker stats --no-stream` также является только мгновенным снимком.

### 21.3. PostgreSQL и control-контейнеры

Для `develop` путь Compose известен из управляемого runtime:

```bash
CONTROL_COMPOSE=/opt/spiritvpn/control/develop/compose.yml

sudo docker compose -f "$CONTROL_COMPOSE" ps -a
sudo docker compose -f "$CONTROL_COMPOSE" exec -T postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Явное указание пользователя и базы не создаёт ложную запись PostgreSQL вида
`role "root" does not exist`, возникающую при запуске `pg_isready` от root без
параметров.

Состояние, OOM и перезапуски каждого control-контейнера:

```bash
for id in $(sudo docker compose -f "$CONTROL_COMPOSE" ps -aq); do
  sudo docker inspect \
    --format 'name={{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} error={{json .State.Error}}' \
    "$id"
done
```

Фильтр аварийных сообщений PostgreSQL:

```bash
sudo docker compose -f "$CONTROL_COMPOSE" \
  logs --no-color --timestamps --since 6h postgres 2>&1 |
grep -Ei \
  'FATAL|PANIC|out of memory|terminated by signal|database system|could not|no space|I/O error|corrupt|recovery|shutdown' |
tail -n 300
```

`database system was not properly shut down` с последующим `automatic recovery`
и `ready to accept connections` означает, что PostgreSQL был прерван, успешно
восстановил WAL и сейчас работает. Это ещё не доказывает, что база была причиной
остановки. Сопоставьте timestamp с загрузкой всего хоста.

### 21.4. OOM, диск, ядро и failed units

```bash
sudo journalctl -k --since '6 hours ago' --no-pager |
grep -Ei \
  'out of memory|oom-kill|oom_reaper|killed process|memory cgroup|segfault|I/O error|no space'

df -hT
df -ih
systemctl --failed --no-pager
sudo find /sys/fs/pstore -maxdepth 1 -type f -printf '%f %s bytes\n'
```

Пустой текущий kernel-фильтр исключает OOM только в просмотренном интервале и
загрузке. Для аварии до reboot обязательно проверяется предыдущая загрузка.

### 21.5. История загрузок и причина перезагрузки

```bash
sudo journalctl --list-boots
sudo last -xF | head -n 30
sudo journalctl -b -1 --no-pager -o short-iso -n 300
```

Ошибки ядра в предыдущей загрузке:

```bash
sudo journalctl -b -1 -k --no-pager -o short-iso |
grep -Ei \
  'out of memory|oom|killed process|panic|watchdog|soft lockup|hard lockup|segfault|I/O error|reset|thermal|machine check|ext4|xfs|nvme|blk_update' |
tail -n 200
```

Инициированное завершение:

```bash
sudo journalctl -b -1 --no-pager -o short-iso |
grep -Ei \
  'reboot|shutdown|poweroff|systemd-shutdown|reached target.*shutdown|systemd-logind|watchdog|oomd|Received SIGINT' |
tail -n 200
```

Интерпретация:

- `Received SIGINT` у PID 1 обычно означает Ctrl+Alt+Del через консоль или
  гипервизор;
- последовательная остановка targets, Docker и SSH означает инициированный
  shutdown, а не внезапный kernel crash;
- обрыв журнала без shutdown указывает на hard reset, зависание VM, потерю
  питания или действие провайдера;
- `panic`, `watchdog`, `lockup`, OOM или I/O error требуют отдельной ветки
  расследования;
- действия панели VPS и гипервизора подтверждаются только журналом провайдера.

### 21.6. Поиск источника постоянной загрузки CPU

Сначала подтвердите насыщение CPU:

```bash
nproc
uptime
vmstat 1 10
```

Если число `r` не меньше числа vCPU, `id` близок к нулю, а `us` близок к 100,
CPU занят пользовательским кодом. Если Docker не объясняет нагрузку, сравните
все cgroups:

```bash
sudo docker stats
```

В другой SSH-сессии:

```bash
sudo systemd-cgtop -b -d 1 -n 10 --cpu=percentage --depth=6
```

Для поиска конкретного процесса и потока:

```bash
sudo pidstat -u -t -p ALL 1 10
```

Если `pidstat` не установлен, не устанавливайте пакет во время первичного
расследования. Используйте имеющийся `top`:

```bash
sudo top -b -H -d 1 -n 10 -o %CPU -w 200
```

CPU cgroup может достигать 200% на машине с двумя vCPU: 100% соответствует
одному полностью занятому ядру. Высокая нагрузка host-level service при низкой
нагрузке Docker означает, что источник находится вне контейнеров.

### 21.7. Проверка неизвестного systemd unit

Имя unit берётся из `systemd-cgtop`; не подставляйте в этот блок имя
доверенного сервиса без проверки. Все команды ниже read-only:

```bash
UNIT='имя-неизвестного-unit.service'
PID=$(sudo systemctl show "$UNIT" -p MainPID --value)
CGROUP=$(sudo systemctl show "$UNIT" -p ControlGroup --value)
FRAGMENT=$(sudo systemctl show "$UNIT" -p FragmentPath --value)

sudo systemctl status "$UNIT" --no-pager -l
sudo systemctl cat "$UNIT" --no-pager

sudo systemctl show "$UNIT" \
  -p FragmentPath \
  -p UnitFileState \
  -p User \
  -p Group \
  -p MainPID \
  -p ControlGroup \
  -p ExecStart \
  -p ActiveEnterTimestamp \
  -p ExecMainStartTimestamp

echo "PID=$PID"
sudo ps -p "$PID" -o pid,ppid,user,lstart,etime,%cpu,%mem,rss,args
sudo ps -T -p "$PID" -o pid,tid,psr,stat,%cpu,comm
sudo systemd-cgls "$CGROUP"

EXE=$(sudo readlink -f "/proc/$PID/exe")
echo "EXE=$EXE"
sudo sha256sum "/proc/$PID/exe"
sudo stat "$EXE" "$FRAGMENT"
sudo dpkg-query -S "$EXE" "$FRAGMENT" 2>/dev/null || true

for member_pid in $(sudo cat "/sys/fs/cgroup$CGROUP/cgroup.procs"); do
  sudo ss -tpn | grep -F "pid=$member_pid," || true
  sudo ss -upn | grep -F "pid=$member_pid," || true
done

sudo journalctl -b -u "$UNIT" --no-pager -o short-iso
```

Признаки критического unmanaged drift:

- unit отсутствует в Git и доверенных пакетах;
- работает от root и включён в автозагрузку;
- исполняет файл из корня, временного или скрытого каталога;
- загружает payload из сети;
- поддерживает постоянное исходящее соединение;
- перезапускается безусловно;
- процесс виден cgroup, но отсутствует в обычном `ps`;
- журнал содержит признаки майнинга, прокси, сканирования или удалённого
  управления.

Если процесс виден через cgroup, но скрыт от `ps`, проверьте возможный
user-space process hider и целостность базовых пакетов:

```bash
sudo test -f /etc/ld.so.preload && sudo cat /etc/ld.so.preload
sudo dpkg -V procps coreutils systemd openssh-server
```

Вывод этих команд является индикатором, а не доказательством чистоты: после
root-компрометации сама ОС и её утилиты больше не являются доверенной базой.

### 21.8. SSH: фактическая политика и история входов

Желаемая политика роли `common` отключает password и keyboard-interactive
authentication, а root допускает только по ключу. Проверяется не отдельный
файл, а итоговая конфигурация `sshd` со всеми `Include` и `Match`:

```bash
sudo sshd -T \
  -C user=root,host="$(hostname)",addr="${SSH_CONNECTION%% *}" |
grep -E \
  '^(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|permitrootlogin|authenticationmethods|usepam) '
```

Безопасный ожидаемый результат содержит:

```text
passwordauthentication no
kbdinteractiveauthentication no
permitrootlogin prohibit-password
```

`without-password` является прежним синонимом `prohibit-password`.
`authenticationmethods any` не включает уже запрещённые password-методы, а
`UsePAM yes` сам по себе не включает password authentication.

История успешных парольных входов без вывода адресов:

```bash
sudo journalctl -u ssh -u sshd --no-pager -o short-iso |
sed -nE 's/^([^ ]+).*Accepted password for ([^ ]+).*/\1 user=\2/p'
```

Полная строка нужна только локально для сопоставления source address с известной
операторской сетью:

```bash
sudo journalctl -u ssh -u sshd --no-pager -o short-iso |
grep -E 'Accepted (password|publickey) for root|Failed password for root'
```

Наличие локального пароля root:

```bash
sudo passwd -S root
sudo ausearch -m USER_CHAUTHTOK -i 2>/dev/null | tail -n 100
```

Статус `P` означает, что локальный password hash существует. При выключенном
`PasswordAuthentication` он не принимается SSH, но остаётся применимым через
консоль. Исторический `Accepted password` доказывает, что в момент входа
парольный метод был разрешён. Неизвестный source address считается возможным
вектором проникновения.

### 21.9. Сетевой след и границы проверки

Сетевое расследование отвечает на четыре вопроса:

1. Что слушает входящие соединения?
2. Какие процессы имеют исходящие и установленные соединения?
3. Через какой интерфейс и route идёт трафик?
4. Разрешён ли этот путь желаемым состоянием и firewall?

Read-only снимок:

```bash
sudo ss -lntup
sudo ss -tpn
sudo ss -upn
sudo nft list ruleset
ip -br address
ip route show table all
sudo wg show
```

Для конкретного подозрительного процесса используйте PID из systemd unit:

```bash
sudo ss -tpn | grep -F "pid=$PID," || true
sudo ss -upn | grep -F "pid=$PID," || true
```

При разборе не публикуйте фактические addresses, endpoints, public keys и
порты. В отчёте используйте логический объект и описание направления, например
«неуправляемое исходящее соединение host-level root service во внешнюю сеть».

Обычный ожидаемый поток управляющего сервера включает:

- входящий операторский SSH только по разрешённому пути и ключу;
- управляющий WireGuard;
- внутренние control и observability listeners на объявленных адресах;
- исходящие обращения к GitHub, registry, DNS, time и явно объявленным внешним
  интеграциям;
- Docker bridge и связанные nftables chains.

Неожиданными считаются публичный management listener, парольный SSH, внешний
listener неизвестного процесса, неизвестный WireGuard peer и постоянное
исходящее соединение unmanaged root service. Ручное добавление firewall rule во
время аудита запрещено: сначала фиксируется соединение, затем согласуется
изоляция, а постоянная политика получает владельца в Git.

### 21.10. Какие Linux-компоненты участвуют

| Компонент | Роль в инциденте и расследовании |
|---|---|
| kernel scheduler и `/proc` | CPU accounting, memory pressure, OOM и список процессов |
| `systemd` | Жизненный цикл units, cgroups, shutdown и автозагрузка |
| `systemd-journald` | Временная шкала boot, units, kernel и shutdown |
| `sshd` и `systemd-logind` | Методы входа, сессии и консольные события |
| `auditd` | Изменение credentials, запуск команд и security events при наличии правил |
| `qemu-guest-agent` | Канал действий и мониторинга со стороны гипервизора VPS |
| `dockerd` и `containerd` | Жизненный цикл control, Vault и observability containers |
| PostgreSQL | Crash recovery после прерывания хоста; не обязательно причина сбоя |
| bot, backend и tunnel | Прикладная часть control plane и возможные потребители CPU/RAM |
| Prometheus, Grafana, Loki, Alloy и exporters | Метрики, alerts, журналы и их собственное потребление ресурсов |
| nftables и Docker chains | Входная, транзитная и контейнерная фильтрация |
| WireGuard и routing | Управляющая связность и обратный маршрут |
| resolver и DNS | Разрешение разрешённых и неуправляемых внешних endpoints |

Высокая нагрузка PostgreSQL видна в его container cgroup. Высокая нагрузка
неизвестного `system.slice/<unit>` является host-level процессом и не появится в
`docker stats`.

### 21.11. Подтверждение причины и статус объекта

Вывод должен разделять первопричину и последствия. Пример структуры:

| Объект | Declared state | Actual state | Статус | Риск | Следующее действие | Владелец |
|---|---|---|---|---|---|---|
| Управляющий сервер | Рабочий host без автоматического reboot | Инициированный shutdown всего host | `drifted` или внешнее событие | Прерывание control plane | Проверить журнал провайдера | Provider boundary + platform runbook |
| PostgreSQL | Healthy control service | Прерван host shutdown, recovery завершён | `match` после recovery | Краткая недоступность | Проверить backup и graceful stop | `roles/control_runtime` |
| Неизвестный unit | Отсутствует | Enabled host-level root service | `unmanaged` | Критический | Сдерживание и rebuild | Не принимать в Git |
| SSH | Только ключи | Эффективная политика и исторические методы входа | `match` или `drifted` | Несанкционированный доступ | Сопоставить источники, затем ротация | `roles/common` и platform SOPS contract |
| Исходящее соединение неизвестного unit | Отсутствует | Постоянный внешний трафик | `unmanaged` | Командный канал или exfiltration | Изоляция после фиксации | Не принимать в Git |

Обнаруженный вредоносный unit нельзя добавлять в Ansible или desired state даже
для последующего удаления. Git должен описывать доверенную конфигурацию чистого
хоста, а не закреплять артефакт компрометации.

### 21.12. Сдерживание подтверждённого вредоносного unit

Этот этап изменяет сервер. Он допустим только после подтверждения, что unit
неуправляемый и вредоносный, фиксации его unit file, executable path, hash,
timestamp, журнала и сетевых соединений. Если доступен snapshot провайдера,
сначала создайте forensic snapshot.

Минимальное обратимое сдерживание без удаления артефактов:

```bash
UNIT='имя-подтверждённого-вредоносного-unit.service'

sudo systemctl disable --now "$UNIT"
sudo systemctl mask "$UNIT"

sudo systemctl is-active "$UNIT"
sudo systemctl is-enabled "$UNIT"
vmstat 1 5
```

Ожидается `inactive`, `masked` и возврат CPU idle. Не удаляйте unit, payload и
журналы до решения о сохранении доказательств. Не считайте остановку очисткой:
root-компрометация означает, что доверять оставшейся ОС нельзя.

Дальнейший безопасный путь:

1. Изолировать скомпрометированный сервер после обеспечения отдельного канала
   восстановления.
2. Зафиксировать snapshot и журналы провайдера.
3. Развернуть новый управляющий сервер из чистого образа текущим Git/Ansible.
4. Восстановить PostgreSQL и Vault только из проверенных резервных копий.
5. Отдельно согласовать ротацию доступных старому хосту SSH, WireGuard, PKI,
   Vault и deployment credentials.
6. Выполнить полный read-only drift/readiness audit нового хоста.
7. Удалить скомпрометированный VPS только после подтверждения восстановления и
   сохранения необходимых доказательств.

Ротация и удаление являются отдельными опасными процедурами и не выполняются
автоматически этим runbook.

## 22. Аудит расхождений без изменений

Аудит должен сравнивать фактическое состояние с Git и выдавать матрицу:

| Поле | Содержание |
|---|---|
| Объект | Логический server/service ID |
| Declared state | Проекция Git без раскрытия чувствительных значений |
| Actual state | Прочитанное состояние runtime |
| Статус | `match`, `drifted`, `unmanaged`, `unreachable` |
| Риск | Влияние расхождения |
| Исправление | Изменение Git или отдельная ceremony |
| Владелец | Конкретный desired file, role или playbook |

Проверки могут читать containers, image IDs, systemd, packages, nftables,
sysctl, SSH, WireGuard, routes, certificates, logs, metrics и внешнюю
поверхность. Результат не должен содержать decrypted addresses, domains, ports,
certificate bodies, keys или secret values.

Нельзя автоматически:

- копировать найденный config в `desired/`;
- удалять unmanaged service;
- закрывать порт;
- менять firewall, route или certificate;
- перезапускать service;
- удалять старый peer.

Сначала отчёт и согласование, затем изменение Git и штатный reconcile.

## 23. Резервное копирование и восстановление

Критические данные вне Git:

```text
Vault recovery material
Vault Raft snapshots
environment CA state
/var/lib/spiritvpn/fleetctl
host-local private keys
runner-local SOPS identity
```

Vault snapshot создаётся wrapper-командой и должен быть немедленно вынесен во
внешнее зашифрованное хранилище. Snapshot на том же management VPS не является
backup.

Потеря `/var/lib/spiritvpn/fleetctl` означает потерю manifest revision allocator
и resume records. Нельзя молча начинать revision с единицы для уже непустого
backend.

Recovery считается готовым только после restore drill на отдельной машине.
Git восстанавливает desired state и automation, но не заменяет backup Vault,
CA и host identities.

## 24. Известные эксплуатационные границы

На текущем `main`:

- provider create/destroy остаётся ручным;
- автоматический deployment запускается событием Git, а не расписанием;
- fleet coordinator не выполняет полный reconcile всех nodes при пустом Git
  impact;
- lifecycle динамических fleet WireGuard peer fragments ещё не полностью
  Git-owned;
- certificate renewal timer создаёт marker, но автоматический collector/signing
  flow ещё не завершён;
- DNS apply отделён от fleet deployment и не удаляет records;
- prod не имеет автоматического approval/apply path;
- destructive operations всегда ручные;
- backup и restore требуют отдельной операторской дисциплины.

Эти пункты являются явными ограничениями, а не разрешением на постоянную ручную
конфигурацию серверов.

## 25. Короткий чек-лист оператора

Перед изменением:

- рабочее дерево чистое;
- `main` синхронизирован с `origin/main`;
- выбран правильный environment;
- понятен владелец изменяемого значения;
- SOPS identity доступна локально;
- destructive impact отсутствует либо отдельно согласован.

Перед apply:

- известен полный source SHA;
- SHA проверен CI и достижим из `main`;
- понятен deployment baseline;
- Vault unsealed;
- нужные `secret://` references заполнены;
- platform access contract явно применён;
- runner и management overlay доступны;
- для первого или повторного запуска правильно выбраны `initial` и `resume`.

После apply:

- readiness прошёл;
- backend вернул `APPLIED` или `IDEMPOTENT`;
- deployment ref продвинулся ожидаемым compare-and-swap;
- alerts, metrics и logs поступают;
- временные plans и resolved files удалены;
- actual state не был записан обратно в Git.
