#!/usr/bin/env bash
# Reads the registry credential out of the environment and writes the npm line that
# matches it. Shared by the publish step and the hardware-verification step: both write
# an .npmrc, and a second copy of this logic would drift from the first.
#
# npm looks credentials up by the registry URL it talks to, path included, so the key is
# the nerf-dart form of that URL ("//host/repository/name/"). A key without the
# trailing slash makes npm drop the last segment and never match the request.
setup_auth() {
  local registry="$1" token_env="$2" auth_kind="$3"
  [ -n "$token_env" ] || return 0
  if [ -z "${!token_env:-}" ]; then
    log "no credential in $token_env; talking to $registry unauthenticated"
    return 0
  fi
  # npm looks credentials up by the registry URL it talks to, path included:
  # "//host/repository/name/" is the key for a registry at .../repository/name/.
  local dart="${registry#*://}"
  dart="${dart%%\?*}"
  dart="//$dart"
  [ "${dart: -1}" = "/" ] || dart="$dart/"
  if [ "$auth_kind" = "basic" ]; then
    # Basic credentials: base64 of user:password, as the registry asks for it
    # (WWW-Authenticate: Basic).
    printf '%s:_auth=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  else
    printf '%s:_authToken=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  fi
  log "auth configured for $dart via $token_env ($auth_kind)"
}
