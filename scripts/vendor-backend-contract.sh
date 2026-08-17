#!/usr/bin/env bash
# Refresh the vendored copy of the normative backend contract from the SpiritVPN
# repository, pinned to an explicit commit. The contract is authoritative for
# backend-owned behaviour; this repository consumes it and must conform to it.
# See contracts/backend/README.md for what to re-check after an update.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_REPO="${SOURCE_REPO:-$REPO/../SpiritVPN}"
SOURCE_PATH="dev/BACKEND_DOMAIN_AGREEMENTS.md"
OUT="$REPO/contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md"

SHA="${1:-}"
[ -n "$SHA" ] || { echo "usage: $0 <commit-sha>   (SOURCE_REPO=$SOURCE_REPO)" >&2; exit 2; }
[ -d "$SOURCE_REPO/.git" ] || { echo "not a git repository: $SOURCE_REPO" >&2; exit 2; }

# Resolve to a full SHA and fail loudly on an unknown or ambiguous revision.
FULL_SHA="$(git -C "$SOURCE_REPO" rev-parse --verify "${SHA}^{commit}")"
DATE="$(git -C "$SOURCE_REPO" log -1 --format=%cs "$FULL_SHA")"
git -C "$SOURCE_REPO" cat-file -e "${FULL_SHA}:${SOURCE_PATH}" 2>/dev/null \
  || { echo "$SOURCE_PATH does not exist at $FULL_SHA" >&2; exit 2; }

# The provenance banner is the ONLY local difference from the source; keep it at
# exactly 8 lines so the diff recipe in contracts/backend/README.md stays valid.
{
  printf '%s\n' \
    '<!-- ВЕНДОРЕННАЯ КОПИЯ — НЕ РЕДАКТИРОВАТЬ ЗДЕСЬ -->' \
    '' \
    "> **Вендоренная копия.** Источник: \`SpiritTechDevelopment/SpiritVPN\`," \
    "> файл \`${SOURCE_PATH}\`, коммит \`${FULL_SHA:0:8}\` (${DATE})." \
    '> Правки вносятся в репозитории-источнике и подтягиваются сюда целиком —' \
    '> см. [README.md](README.md). Единственное локальное отличие от источника —' \
    '> эта врезка.' \
    ''
  git -C "$SOURCE_REPO" show "${FULL_SHA}:${SOURCE_PATH}"
} > "$OUT"

echo "vendored ${SOURCE_PATH} @ ${FULL_SHA:0:8} (${DATE}) -> ${OUT#"$REPO"/}"
echo
echo "Теперь перепроверь (contracts/backend/README.md):"
echo "  1. инварианты компилятора в fleetctl"
echo "  2. схемы в contracts/desired-state/ и contracts/schemas/"
echo "  3. открытые решения в docs/architecture/INFRA_TECHNICAL_SPEC.md §23"
echo "  4. дельту к бэкенду там же, §24"
