#!/bin/sh
set -eu

printf 'APP_MODE=%s\n' "${APP_MODE:-"(unset)"}"
printf 'NODE_VERSION=%s\n' "$(node --version)"
