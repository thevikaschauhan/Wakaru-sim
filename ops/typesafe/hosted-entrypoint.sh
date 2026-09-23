#!/bin/sh
set -eu
umask 077
: "${TYPESAFE_DEPLOYMENT_JSON:?Reviewed hosted deployment profile required}"
: "${TYPESAFE_DEPLOYMENT_FILE:?Absolute deployment profile path required}"
case "$TYPESAFE_DEPLOYMENT_FILE" in /tmp/typesafe-active.json) ;; *) echo 'Unexpected hosted control path' >&2; exit 1;; esac
printf '%s' "$TYPESAFE_DEPLOYMENT_JSON" > "${TYPESAFE_DEPLOYMENT_FILE}.new"
mv "${TYPESAFE_DEPLOYMENT_FILE}.new" "$TYPESAFE_DEPLOYMENT_FILE"
unset TYPESAFE_DEPLOYMENT_JSON
export TYPESAFE_PRELAUNCH=1
exec "$@"
