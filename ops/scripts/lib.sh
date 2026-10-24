#!/usr/bin/env bash
# Shared helpers for ops/scripts. Source it; do not run it.

# load_env FILE
#
# Export KEY=VALUE pairs from a compose-style .env file WITHOUT shell
# expansion: a value containing `$`, backticks or quotes is taken literally
# (sourcing the file would expand or execute it). Rules, matching Docker
# Compose's parser for the subset we use:
#   - blank lines and lines starting with # are skipped
#   - the value is everything after the first `=`
#   - for an unquoted value, ` #` (whitespace then #) starts a comment
#   - a value wrapped in matching single or double quotes is unwrapped
load_env() {
  local file="$1" line key value
  [[ -f "$file" ]] || { echo "missing env file: ${file}" >&2; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"          # ltrim
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "$value" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
      value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%%[[:space:]]#*}"                  # strip inline comment
      value="${value%"${value##*[![:space:]]}"}"       # rtrim
    fi
    export "$key=$value"
  done < "$file"
}
