# Shared helpers for the deepfake-voice-inference launch scripts.
# Sourced by every scripts/*.sh — not meant to be run directly.
#
# Responsibilities:
#   - resolve REPO_ROOT
#   - load defaults (config.env.example) then real overrides (config.env)
#   - activate the project virtualenv and export the runtime environment
#   - expose run() which either execs the command or, when DRYRUN=1, prints it

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT

# Defaults first, then the gitignored real config overrides them. Both are
# sourced so values may reference $REPO_ROOT / $HOME and each other.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/config.env.example"
if [[ -f "$SCRIPT_DIR/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/config.env"
fi

# Runtime environment shared by all python entry points.
export PYTHONPATH="$REPO_ROOT"
export TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1

# Activate the project venv unless the caller already provided a python.
activate_venv() {
  if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.venv/bin/activate"
  fi
}

# Assemble the server-side signature verification flags into the named array.
# Usage: signature_verifier_flags FLAGS_ARRAY_NAME
signature_verifier_flags() {
  local -n _out="$1"
  _out=(--signature-policy "$SIGNATURE_POLICY")
  local trusted="$SIGNATURE_TRUSTED_KEY"
  if [[ -z "$trusted" && -n "$SIGNATURE_KEY" ]]; then
    trusted="$SIGNATURE_KEY_ID=$SIGNATURE_KEY"
  fi
  if [[ "$SIGNATURE_POLICY" != "off" && -n "$trusted" ]]; then
    _out+=(--signature-trusted-key "$trusted")
  fi
}

# Assemble the client-side signature signing flags into the named array.
# Usage: signature_sender_flags FLAGS_ARRAY_NAME
signature_sender_flags() {
  local -n _out="$1"
  _out=()
  if [[ -n "$SIGNATURE_KEY" ]]; then
    _out=(--signature-key "$SIGNATURE_KEY" --signature-key-id "$SIGNATURE_KEY_ID")
  fi
}

# Run a command, or print it when DRYRUN=1 (for verification without launching).
run() {
  if [[ "${DRYRUN:-0}" == "1" ]]; then
    printf '%q ' "$@"
    printf '\n'
    return 0
  fi
  exec "$@"
}
