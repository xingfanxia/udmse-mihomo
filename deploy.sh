#!/usr/bin/env bash
# Compatibility entrypoint: prepare only, never starts or enables routing.
set -euo pipefail
exec bash "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"
