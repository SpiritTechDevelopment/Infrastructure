#!/usr/bin/env bash
# Показать, что пошло не так в последнем прогоне workflow.
#
#   scripts/deploy-log.sh                 последний прогон любого workflow
#   scripts/deploy-log.sh fleet-deploy    последний прогон конкретного
#   scripts/deploy-log.sh fleet-deploy 2  предпоследний, и так далее
#
# Логи берутся job-level эндпоинтом. `gh run view --log` тянет zip-архив через
# results-receiver, который с рабочей станции блокируется и висит до таймаута.
set -euo pipefail

repository="${SPIRITVPN_REPO:-SpiritTechDevelopment/Infrastructure}"
workflow="${1:-}"
back="${2:-1}"

list=(run list --limit "$back" --json databaseId,name,conclusion,headSha,createdAt)
if [[ -n "$workflow" ]]; then
  list+=(--workflow "$workflow")
fi

index=$((back - 1))
meta="$(gh "${list[@]}" --jq ".[$index]")"
if [[ -z "$meta" || "$meta" == "null" ]]; then
  printf 'нет прогонов%s\n' "${workflow:+ для $workflow}" >&2
  exit 1
fi

read -r run_id name conclusion sha created <<<"$(
  printf '%s' "$meta" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["databaseId"], d["name"], d["conclusion"] or "in_progress", d["headSha"][:7], d["createdAt"])'
)"

printf '=== %s  %s  %s  %s ===\n' "$name" "$conclusion" "$sha" "$created"

job="$(gh api "repos/$repository/actions/runs/$run_id/attempts/1/jobs" --jq '.jobs[0].id')"
log="$(mktemp)"
trap 'rm -f -- "$log"' EXIT
gh api "repos/$repository/actions/jobs/$job/logs" >"$log" 2>&1

# Режим запуска. check и apply дают одинаково зелёный recap, поэтому глазами их
# не различить — а разница между "показал бы диф" и "применил" максимальная.
printf '\n--- режим ---\n'
grep -oE 'REQUESTED_MODE: [a-z-]+' "$log" | tail -1 || echo '(не найден)'

# Падения задач Ansible: сообщение целиком, оно обычно и есть диагноз.
printf '\n--- падения задач ---\n'
if ! grep -hoE '(fatal|failed): \[[^]]+\].*|.*UNREACHABLE!.*' "$log" | tail -5; then
  echo '(нет)'
fi

printf '\n--- итог по хостам ---\n'
if ! sed -n "/PLAY RECAP/,/^$/p" "$log" | grep -E 'ok=' | sed 's/^[0-9T:.Z-]* //'; then
  echo '(recap отсутствует — до Ansible не дошло)'
fi

# Ошибки координатора и проверок исполнителя живут ЗДЕСЬ, а не среди задач
# Ansible: это обычные строки в stderr после последнего recap. Чистый recap при
# красном прогоне означает ровно это.
printf '\n--- после последнего recap ---\n'
recap_line="$(grep -n 'PLAY RECAP' "$log" | tail -1 | cut -d: -f1 || true)"
if [[ -n "$recap_line" ]]; then
  sed -n "$((recap_line)),+12p" "$log" |
    grep -vE 'PLAY RECAP|ok=|^\s*$' |
    grep -vE '##\[(group|endgroup)\]|^\S+ +shell: |Node 20 is being deprecated|Post job cleanup' |
    sed 's/^[0-9T:.Z-]* //' | head -6
else
  printf '(recap не найден; последние строки лога)\n'
  tail -12 "$log" | sed 's/^[0-9T:.Z-]* //'
fi

printf '\n--- ошибки шагов workflow ---\n'
if ! grep -hoE '##\[error\].*' "$log" | tail -3; then
  echo '(нет)'
fi
