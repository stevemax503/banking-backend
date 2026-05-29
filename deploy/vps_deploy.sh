#!/usr/bin/env bash
set -euo pipefail

# Deploy banking-backend on the VPS (native systemd + nginx).
#
# What it does:
# - Pull latest code from GitHub
# - Install Python deps (requirements.txt) into the existing venv
# - Run makemigrations + migrate
# - Collect static files
# - Restart gunicorn + celery + celery beat
#
# Assumptions:
# - Repo lives at /opt/banking-backend
# - Virtualenv lives at /opt/banking-backend/.venv
# - systemd units are already installed/enabled:
#   banking-backend-gunicorn, banking-backend-celery, banking-backend-celery-beat

REPO_DIR="${REPO_DIR:-/opt/banking-backend}"
VENV_DIR="${VENV_DIR:-}"

if [[ -z "${VENV_DIR}" ]]; then
  if [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    VENV_DIR="${REPO_DIR}/.venv"
  elif [[ -x "${REPO_DIR}/venv/bin/python" ]]; then
    VENV_DIR="${REPO_DIR}/venv"
  fi
fi

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

# Resolve requirements after pull (paths may not exist until latest code is on disk).
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
  echo "Expected one of:" >&2
  echo "  - ${REPO_DIR}/.venv/bin/python" >&2
  echo "  - ${REPO_DIR}/venv/bin/python" >&2
  echo "" >&2
  echo "Create it first (example): python3 -m venv ${REPO_DIR}/venv" >&2
  exit 1
fi

echo "==> Install backend dependencies"
"${PIP_BIN}" install --upgrade pip
if [[ -z "${REQ_FILE}" || ! -f "${REQ_FILE}" ]]; then
  echo "ERROR: requirements file not found." >&2
  echo "Expected one of:" >&2
  echo "  - ${REPO_DIR}/requirements/prod.txt" >&2
  echo "  - ${REPO_DIR}/requirements.txt" >&2
  echo "  - ${REPO_DIR}/requirements/base.txt" >&2
  echo "  - ${REPO_DIR}/requirements/requirements.txt" >&2
  exit 1
fi
"${PIP_BIN}" install -r "${REQ_FILE}"

echo "==> Django migrations"
"${PYTHON_BIN}" manage.py makemigrations
"${PYTHON_BIN}" manage.py migrate

echo "==> Collect static"
"${PYTHON_BIN}" manage.py collectstatic --noinput

echo "==> Restart services"
sudo systemctl restart banking-backend-gunicorn
sudo systemctl restart banking-backend-celery
sudo systemctl restart banking-backend-celery-beat

echo "==> Status"
sudo systemctl --no-pager status banking-backend-gunicorn banking-backend-celery banking-backend-celery-beat | sed -n '1,60p'

echo "==> Deploy complete"

