#!/bin/bash
# =============================================================
# pyscript_sync_from_urls.sh  v1.4.0
# Sync files / folders tu GitHub ve /config/pyscript
#
# Config: /config/scripts/pyscript_sync.conf  (load tu dong)
# Su dung: bash script.sh [--update] [--debug] [--token <token>]
#   CLI args se ghi de gia tri trong .conf
# =============================================================
_version="1.4.0"

function _info   { echo "$@"; }
function _dbg    { [ "${_pyscript_sync_debug}" == "true" ] && echo "$@"; }
function _newline { echo ""; }

_tmp=""
trap '[[ -n "${_tmp}" ]] && rm -f "${_tmp}"' EXIT

# =============================================================
# LOAD CONFIG FILE
# Tim file .conf cung thu muc voi script nay
# =============================================================
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CONF_FILE="${_SCRIPT_DIR}/pyscript_sync.conf"

# Gia tri mac dinh (neu khong co .conf)
_pyscript_sync_dir="/config/pyscript"
_pyscript_sync_manifest="${_pyscript_sync_dir}/_sources.txt"
_pyscript_sync_auto_update="false"
_pyscript_sync_token=""
_pyscript_sync_reload="true"
_pyscript_sync_curl_options="--silent"
_pyscript_sync_debug="false"

if [ -f "${_CONF_FILE}" ]; then
  # shellcheck source=/dev/null
  source "${_CONF_FILE}"
  _info "> config   : ${_CONF_FILE}"
else
  _info "> config   : (khong tim thay ${_CONF_FILE}, dung mac dinh)"
fi

# Alias ngan gon de dung trong script
PYSCRIPT_DIR="${_pyscript_sync_dir}"
MANIFEST="${_pyscript_sync_manifest}"

# =============================================================
# CLI ARGS — ghi de .conf neu truyen vao
# =============================================================
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --update) _pyscript_sync_auto_update="true" ;;
    --debug)  _pyscript_sync_debug="true" ;;
    --token)  _pyscript_sync_token="$2"; shift ;;
    --no-reload) _pyscript_sync_reload="false" ;;
    *) ;;
  esac
  shift
done

# Alias sau khi merge CLI args
_do_update="${_pyscript_sync_auto_update}"
_gh_token="${_pyscript_sync_token}"
_do_reload="${_pyscript_sync_reload}"

# =============================================================
# HEADER
# =============================================================
_info "> pyscript sync ${_version}"
_info "> dir      : ${PYSCRIPT_DIR}"
_info "> manifest : ${MANIFEST}"
_info "> update   : ${_do_update}"
_info "> reload   : ${_do_reload}"
[ -n "${_gh_token}" ] && _info "> token    : ***"
_newline

# =============================================================
# URL DETECTION
# =============================================================
function _detect_url_type() {
  local url="$1"
  [[ "$url" == *"github.com"*"/blob/"*       ]] && echo "file"   && return
  [[ "$url" == *"raw.githubusercontent.com"* ]] && echo "file"   && return
  [[ "$url" == *"raw.github.com"*            ]] && echo "file"   && return
  [[ "$url" == *"github.com"*"/tree/"*       ]] && echo "folder" && return
  [[ "$url" == *"api.github.com/repos"*      ]] && echo "folder" && return
  echo "$url" | grep -qE '\.(py|yaml|yml|json|txt|sh|js|lua|conf|cfg|ini|toml|md)(\?.*)?$' \
    && echo "file" && return
  echo "unknown"
}

# =============================================================
# URL CONVERSION
# =============================================================
function _to_raw_url() {
  local url="$1"
  if [[ "$url" == *"github.com"*"/blob/"* ]]; then
    url=$(echo "$url" | sed \
      -e 's#https://github.com/#https://raw.githubusercontent.com/#' \
      -e 's#/blob/#/#')
  fi
  echo "$url"
}

function _to_github_api_url() {
  local url="$1"
  if [[ "$url" =~ ^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(/.+)?$ ]]; then
    local user="${BASH_REMATCH[1]}"
    local repo="${BASH_REMATCH[2]}"
    local ref="${BASH_REMATCH[3]}"
    local path="${BASH_REMATCH[4]#/}"
    if [ -n "$path" ]; then
      echo "https://api.github.com/repos/${user}/${repo}/contents/${path}?ref=${ref}"
    else
      echo "https://api.github.com/repos/${user}/${repo}/contents?ref=${ref}"
    fi
    return 0
  fi
  [[ "$url" == *"api.github.com"* ]] && echo "$url" && return 0
  return 1
}

function _parse_repo_ref() {
  local url="$1"
  if [[ "$url" =~ ^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+) ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}|${BASH_REMATCH[3]}"
  fi
}

# =============================================================
# HTTP — dung _pyscript_sync_curl_options tu .conf
# =============================================================
function _download() {
  local out="$1" url="$2"
  if [ -n "${_gh_token}" ]; then
    curl -L -f -s \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${_gh_token}" \
      -o "$out" "$url"
  else
    curl -L -f -s \
      -H "Accept: application/vnd.github+json" \
      -o "$out" "$url"
  fi
}

function _fetch_api() {
  local url="$1"
  if [ -n "${_gh_token}" ]; then
    curl -L -f -s \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${_gh_token}" \
      "$url"
  else
    curl -L -f -s \
      -H "Accept: application/vnd.github+json" \
      "$url"
  fi
}

# =============================================================
# JSON PARSER
# =============================================================
function _parse_json_files() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '.[] | [.type, .name, (.download_url // ""), .path] | join("|")'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "
import sys, json
data = json.load(sys.stdin)
for i in data:
    print('{}|{}|{}|{}'.format(i['type'], i['name'], i.get('download_url') or '', i['path']))
"
  else
    _info "! Can jq hoac python3 (tren HA OS: apk add jq)"
    return 1
  fi
}

# =============================================================
# XU LY FILE DON LE
# =============================================================
function _process_single_file() {
  local url="$1"
  local relpath="$2"

  total_count=$((total_count+1))

  local raw_url dest
  raw_url="$(_to_raw_url "${url}")"
  dest="${PYSCRIPT_DIR}/${relpath}"

  _info "> ${relpath}"
  _dbg "  url : ${raw_url}"
  _dbg "  dst : ${dest}"

  _tmp="$(mktemp /tmp/pyscript_sync_XXXXXX)"

  if ! _download "${_tmp}" "${raw_url}"; then
    _info "  ! download failed: ${raw_url}"
    rm -f "${_tmp}"; _tmp=""
    fail_count=$((fail_count+1))
    return
  fi

  if [ -f "${dest}" ] && diff -q "${dest}" "${_tmp}" >/dev/null 2>&1; then
    _info "  -> up-to-date"
    rm -f "${_tmp}"; _tmp=""; return
  fi

  if [ "${_do_update}" == "true" ]; then
    mkdir -p "$(dirname "${dest}")"
    cp "${_tmp}" "${dest}"
    _info "  -! updated"
    changed_count=$((changed_count+1))
  else
    _info "  -! changed (them --update hoac dat auto_update=true trong .conf)"
    changed_count=$((changed_count+1))
  fi

  rm -f "${_tmp}"; _tmp=""
}

# =============================================================
# XU LY FOLDER GITHUB
# =============================================================
function _process_github_folder() {
  local folder_url="$1"
  local dest_prefix="${2%/}"
  local recursive="${3:-false}"

  local api_url
  if ! api_url="$(_to_github_api_url "${folder_url}")"; then
    _info "! URL folder khong hop le: ${folder_url}"
    fail_count=$((fail_count+1)); return
  fi

  _info ">> Folder: ${dest_prefix}/"
  _dbg "   api : ${api_url}"

  local api_response
  api_response="$(_fetch_api "${api_url}")"
  if [ $? -ne 0 ]; then
    _info "  ! GitHub API that bai"
    _info "  Neu private repo: dat token trong .conf hoac them --token"
    fail_count=$((fail_count+1)); return
  fi

  if echo "${api_response}" | grep -q '"message"'; then
    local msg
    msg=$(echo "${api_response}" | grep -o '"message":"[^"]*"' | head -1)
    _info "  ! GitHub API loi: ${msg}"
    fail_count=$((fail_count+1)); return
  fi

  local entries
  entries=$(echo "${api_response}" | _parse_json_files) \
    || { fail_count=$((fail_count+1)); return; }

  local repo_ref
  repo_ref="$(_parse_repo_ref "${folder_url}")"
  local base_repo_url="https://github.com/${repo_ref%%|*}"
  local ref="${repo_ref##*|}"

  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    local item_type item_name item_dl item_path
    item_type="$(echo "$entry" | cut -d'|' -f1)"
    item_name="$(echo "$entry" | cut -d'|' -f2)"
    item_dl="$(  echo "$entry" | cut -d'|' -f3)"
    item_path="$(echo "$entry" | cut -d'|' -f4)"

    if [ "${item_type}" == "file" ]; then
      _process_single_file "${item_dl}" "${dest_prefix}/${item_name}"
    elif [ "${item_type}" == "dir" ] && [ "${recursive}" == "true" ]; then
      local sub_url="${base_repo_url}/tree/${ref}/${item_path}"
      _process_github_folder "${sub_url}" "${dest_prefix}/${item_name}" "true"
    else
      _dbg "  >> skip dir: ${item_name} (them |recursive de vao)"
    fi
  done <<< "${entries}"
}

# =============================================================
# ROUTER CHINH
# =============================================================
function _process_url() {
  local url="$1"
  local dest="$2"
  local option="$3"

  local recursive="false"
  [ "${option}" == "recursive" ] && recursive="true"

  local forced_type=""
  [[ "${dest}" == */ ]] && forced_type="folder"

  local url_type
  url_type="$(_detect_url_type "${url}")"

  _dbg "  detect: ${url_type} | dest: ${dest} | forced: ${forced_type:-none}"

  if [ "${forced_type}" == "folder" ] || [ "${url_type}" == "folder" ]; then
    _process_github_folder "${url}" "${dest}" "${recursive}"
  elif [ "${url_type}" == "file" ]; then
    _process_single_file "${url}" "${dest}"
  else
    if _to_github_api_url "${url}" >/dev/null 2>&1; then
      _process_github_folder "${url}" "${dest}" "${recursive}"
    else
      _process_single_file "${url}" "${dest}"
    fi
  fi
}

# =============================================================
# MAIN
# =============================================================
[ ! -d "${PYSCRIPT_DIR}" ] && {
  _info "-! pyscript dir not found: ${PYSCRIPT_DIR}"
  _info "   Tao thu muc hoac sua _pyscript_sync_dir trong .conf"
  exit 1
}
[ ! -f "${MANIFEST}" ] && {
  _info "-! manifest not found: ${MANIFEST}"
  _info "   Tao file hoac sua _pyscript_sync_manifest trong .conf"
  _info "   Format: url|dest  hoac  url|dest/|recursive"
  exit 1
}

changed_count=0
fail_count=0
total_count=0

while IFS= read -r line || [ -n "$line" ]; do
  line="$(echo "$line" | sed 's/\r$//')"
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^[[:space:]]*# ]] && continue

  url="$(    echo "$line" | cut -d'|' -f1 | xargs)"
  dest="$(   echo "$line" | cut -d'|' -f2 | xargs)"
  option="$( echo "$line" | cut -d'|' -f3 | xargs | tr '[:upper:]' '[:lower:]')"

  if [ -z "${url}" ] || [ -z "${dest}" ]; then
    _info "-! dong khong hop le: ${line}"
    fail_count=$((fail_count+1))
    _newline; continue
  fi

  _process_url "${url}" "${dest}" "${option}"
  _newline

done < "${MANIFEST}"

_info "Summary: total=${total_count}, changed=${changed_count}, failed=${fail_count}"

# Reload pyscript
if [ "${_do_update}" == "true" ] && [ "${changed_count}" -gt 0 ] && [ "${_do_reload}" == "true" ]; then
  if command -v ha >/dev/null 2>&1; then
    ha service call pyscript.reload >/dev/null 2>&1 \
      && _info "-> pyscript.reload called" \
      || _info "! pyscript.reload failed"
  else
    _info "-> reload thu cong: Developer Tools > Services > pyscript.reload"
  fi
fi

[ "${fail_count}" -gt 0 ] && exit 1
exit 0
