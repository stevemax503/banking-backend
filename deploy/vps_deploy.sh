#!/usr/bin/env bash
set -euo pipefail

# Deploy banking-backend on the VPS (native systemd + nginx).
#
# What it does:
# - Pull latest code from GitHub
# - Install Python deps into the existing venv
# - Stop gunicorn/celery (frees RAM for migrate + collectstatic)
# - Run migrate (does NOT run makemigrations — commit migrations in git)
# - Collect static files
# - Restart gunicorn + celery + celery beat (always, even if a step failed)
#
# Env overrides:
#   REPO_DIR, VENV_DIR, PYTHON_BIN, PIP_BIN, REQ_FILE
#   COLLECTSTATIC_LIGHT=1  — skip WhiteNoise gzip pass (default: 1, safer on small VPS)
#   SKIP_COLLECTSTATIC=1   — skip collectstatic entirely
#   SKIP_SERVICE_RESTART=1 — do not stop/restart systemd units

REPO_DIR="${REPO_DIR:-/opt/banking-backend}"
VENV_DIR="${VENV_DIR:-}"
COLLECTSTATIC_LIGHT="${COLLECTSTATIC_LIGHT:-1}"
SKIP_COLLECTSTATIC="${SKIP_COLLECTSTATIC:-0}"
SKIP_SERVICE_RESTART="${SKIP_SERVICE_RESTART:-0}"

GUNICORN_UNIT="${GUNICORN_UNIT:-banking-backend-gunicorn}"
CELERY_UNIT="${CELERY_UNIT:-banking-backend-celery}"
CELERY_BEAT_UNIT="${CELERY_BEAT_UNIT:-banking-backend-celery-beat}"

if [[ -z "${VENV_DIR}" ]]; then
  if [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    VENV_DIR="${REPO_DIR}/.venv"
  elif [[ -x "${REPO_DIR}/venv/bin/python" ]]; then
    VENV_DIR="${REPO_DIR}/venv"
  fi
fi

SERVICES_STOPPED=0

stop_app_services() {
  if [[ "${SKIP_SERVICE_RESTART}" == "1" ]]; then
    return 0
  fi
  echo "==> Stop app services (free memory for migrate/collectstatic)"
  sudo systemctl stop "${GUNICORN_UNIT}" "${CELERY_UNIT}" "${CELERY_BEAT_UNIT}" 2>/dev/null || true
  SERVICES_STOPPED=1
  sleep 2
}

restart_app_services() {
  if [[ "${SKIP_SERVICE_RESTART}" == "1" || "${SERVICES_STOPPED}" -eq 0 ]]; then
    return 0
  fi
  echo "==> Restart services"
  sudo systemctl restart "${GUNICORN_UNIT}" "${CELERY_UNIT}" "${CELERY_BEAT_UNIT}"
  echo "==> Status"
  sudo systemctl --no-pager status "${GUNICORN_UNIT}" "${CELERY_UNIT}" "${CELERY_BEAT_UNIT}" | sed -n '1,60p' || true
}

on_exit() {
  local code=$?
  restart_app_services || true
  if [[ "${code}" -ne 0 ]]; then
    echo "" >&2
    echo "Deploy failed (exit ${code})." >&2
    echo "If collectstatic was Killed, the VPS ran out of RAM — add swap or use COLLECTSTATIC_LIGHT=1 (default)." >&2
    exit "${code}"
  fi
}

trap on_exit EXIT

echo "==> Deploy starting"

if [[ ! -d "${REPO_DIR}" ]]; then
  echo "ERROR: REPO_DIR not found: ${REPO_DIR}" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-${VENV_DIR}/bin/python}"
PIP_BIN="${PIP_BIN:-${VENV_DIR}/bin/pip}"
REQ_FILE="${REQ_FILE:-}"

cd "${REPO_DIR}"

echo "==> Pull latest code"
git fetch --all --prune
git checkout main
git pull --ff-only origin main

if [[ -z "${REQ_FILE}" ]]; then
  if [[ -e "${REPO_DIR}/requirements/prod.txt" ]]; then
    REQ_FILE="${REPO_DIR}/requirements/prod.txt"
  elif [[ -e "${REPO_DIR}/requirements.txt" ]]; then
    REQ_FILE="${REPO_DIR}/requirements.txt"
  elif [[ -e "${REPO_DIR}/requirements/base.txt" ]]; then
    REQ_FILE="${REPO_DIR}/requirements/base.txt"
  elif [[ -e "${REPO_DIR}/requirements/requirements.txt" ]]; then
    REQ_FILE="${REPO_DIR}/requirements/requirements.txt"
  elif [[ -e "${REPO_DIR}/requirements" && -f "${REPO_DIR}/requirements" ]]; then
    REQ_FILE="${REPO_DIR}/requirements"
  fi
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: Python venv not found/executable: ${PYTHON_BIN}" >&2
  exit 1
fi

echo "==> Install backend dependencies"
"${PIP_BIN}" install --upgrade pip
if [[ -z "${REQ_FILE}" || ! -f "${REQ_FILE}" ]]; then
  echo "ERROR: requirements file not found." >&2
  exit 1
fi
"${PIP_BIN}" install -r "${REQ_FILE}"

stop_app_services

echo "==> Django migrations"
"${PYTHON_BIN}" manage.py migrate --noinput

if [[ "${SKIP_COLLECTSTATIC}" == "1" ]]; then
  echo "==> Collect static (skipped — SKIP_COLLECTSTATIC=1)"
else
  echo "==> Collect static (COLLECTSTATIC_LIGHT=${COLLECTSTATIC_LIGHT})"
  if ! COLLECTSTATIC_LIGHT="${COLLECTSTATIC_LIGHT}" "${PYTHON_BIN}" manage.py collectstatic --noinput; then
    echo "WARN: collectstatic failed; retrying with COLLECTSTATIC_LIGHT=1..." >&2
    COLLECTSTATIC_LIGHT=1 "${PYTHON_BIN}" manage.py collectstatic --noinput
  fi
fi

trap - EXIT
restart_app_services

echo "==> Deploy complete"
