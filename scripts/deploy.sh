#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="/root/Mimoru"
cd "$PROJECT_DIR"

echo "==> Проверка окружения..."
if [[ ! -f .env ]]; then
  echo "ОШИБКА: $PROJECT_DIR/.env не найден. Деплой остановлен."
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ОШИБКА: на сервере есть локальные изменения или untracked-файлы."
  echo "Сначала сохраните или отмените их, затем повторите деплой:"
  git status --short
  exit 1
fi

echo "==> Получение изменений из GitHub..."
git fetch origin main
git pull --ff-only origin main
DEPLOY_SHA="$(git rev-parse --short HEAD)"
echo "==> Версия для деплоя: $DEPLOY_SHA"

echo "==> Проверка статуса CI для $DEPLOY_SHA..."
# Проверка CI не должна обрывать деплой сама. Раньше здесь стояли
# `set -e` + `pipefail` вместе с голыми grep/sed/curl, и любой их отказ -
# недоступный sed, пустой ответ API - завершал скрипт молча, сразу после
# этого echo: контейнер не пересобирался, но деплой выглядел успешным.
# Поэтому блок выполняется в подshell с отключёнными -e/-o pipefail,
# а единственный случай, когда деплой всё же останавливается, - это
# явный код возврата 3: в CI есть провалившийся прогон.
(
  set +Eeuo pipefail

  REMOTE_URL="$(git remote get-url origin 2>/dev/null)"
  REPO_SLUG="$(printf '%s' "$REMOTE_URL" | sed -e 's#^.*github.com[:/]##' -e 's#\.git$##')"
  if [[ -z "$REPO_SLUG" || "$REPO_SLUG" == "$REMOTE_URL" ]]; then
    echo "ВНИМАНИЕ: не удалось определить репозиторий из remote ($REMOTE_URL). Проверка CI пропущена."
    exit 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "ВНИМАНИЕ: curl не установлен. Проверка CI пропущена."
    exit 0
  fi

  # Без токена GitHub отдаёт анонимные запросы с лимитом 60 в час на IP, и
  # при его исчерпании ответ не содержит поля conclusion. Гейт тогда
  # предупреждает вместо блокировки. Токен поднимает лимит и возвращает
  # полные данные, поэтому проверка снова работает по-настоящему.
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    CI_JSON="$(curl -fsS --max-time 25 \
      -H 'Accept: application/vnd.github+json' \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      "https://api.github.com/repos/$REPO_SLUG/actions/runs?head_sha=$DEPLOY_SHA&per_page=20" 2>/dev/null)"
  else
    CI_JSON="$(curl -fsS --max-time 25 \
      -H 'Accept: application/vnd.github+json' \
      "https://api.github.com/repos/$REPO_SLUG/actions/runs?head_sha=$DEPLOY_SHA&per_page=20" 2>/dev/null)"
  fi
  if [[ -z "$CI_JSON" ]]; then
    echo "ВНИМАНИЕ: не удалось получить статус CI (сеть или лимит API). Проверка пропущена."
    exit 0
  fi

  CI_CONCLUSIONS="$(printf '%s' "$CI_JSON" | grep -o '"conclusion": *"[^"]*"' | sed 's/.*: *"//; s/"$//' | sort -u)"
  if [[ -z "$CI_CONCLUSIONS" ]]; then
    echo "ВНИМАНИЕ: в ответе GitHub API нет поля conclusion. Проверка CI пропущена."
    CI_CONCLUSIONS="нет данных"
  fi

  BLOCKING=""
  for candidate in failure cancelled timed_out action_required startup_failure stale; do
    if printf '%s\n' "$CI_CONCLUSIONS" | grep -qx "$candidate"; then
      BLOCKING="$BLOCKING $candidate"
    fi
  done
  if [[ -n "$BLOCKING" ]]; then
    echo "ОШИБКА: CI для $DEPLOY_SHA неуспешен:$BLOCKING. Деплой остановлен."
    exit 3
  fi
  if [[ "$CI_CONCLUSIONS" == *success* ]]; then
    echo "CI зелёный."
  else
    echo "ВНИМАНИЕ: CI ещё не завершён успешно (результаты: $CI_CONCLUSIONS). Деплой продолжаю."
  fi
)
CI_GATE_STATUS=$?
if [[ $CI_GATE_STATUS -eq 3 ]]; then
  echo "Сначала исправьте CI и дождитесь зелёного, затем повторите."
  exit 1
fi
if [[ $CI_GATE_STATUS -ne 0 ]]; then
  echo "ВНИМАНИЕ: проверка CI завершилась с кодом $CI_GATE_STATUS. Деплой продолжаю."
fi

echo "==> Проверка docker-compose.yml..."
docker compose config -q

echo "==> Запуск PostgreSQL и Redis..."
docker compose up -d postgres redis

echo "==> Сборка новой версии бота без старого кэша..."
docker compose build --no-cache bot

echo "==> Принудительное пересоздание бота и запуск backup-сервиса..."
docker compose up -d --force-recreate bot
docker compose up -d backup

echo "==> Проверка кода внутри запущенного контейнера..."
if ! docker compose exec -T bot python -c 'from app.handlers.moderation_command_modes import _split_command; assert _split_command("Пред\nВ") == ("пред", [], "В")'; then
  # exec fails for any reason: dead container, failed migration, failed preflight,
  # broken import. Show the real cause instead of blaming the reason parser.
  echo "ОШИБКА: проверка кода в контейнере bot не пройдена. Состояние контейнера:"
  docker compose ps -a bot
  echo "--- логи бота ---"
  docker compose logs --tail=120 bot
  exit 1
fi

echo "==> Ожидание готовности бота..."
# `|| true`, иначе непустой код docker compose ps -q обрывает скрипт
# молча, и понятное сообщение о несозданном контейнере ниже не показывается.
BOT_ID="$(docker compose ps -q bot || true)"
if [[ -z "$BOT_ID" ]]; then
  echo "ОШИБКА: контейнер bot не создан."
  docker compose ps
  exit 1
fi

READY=0
for _ in {1..18}; do
  STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$BOT_ID" 2>/dev/null || true)"
  case "$STATUS" in
    healthy)
      READY=1
      break
      ;;
    unhealthy|exited|dead)
      echo "ОШИБКА: bot имеет статус: $STATUS"
      docker compose logs --tail=120 bot
      exit 1
      ;;
  esac
  sleep 5
done

if [[ "$READY" -ne 1 ]]; then
  echo "ОШИБКА: bot не стал healthy за 90 секунд."
  docker compose ps
  docker compose logs --tail=120 bot
  exit 1
fi

echo "==> Деплой завершён успешно: $DEPLOY_SHA"
docker compose ps
echo "==> Последние логи бота:"
docker compose logs --tail=60 bot
