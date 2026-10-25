#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --platform linux/arm64 -t "${PACK_IMAGE:-pack-cell:jazzy}" -f docker/Dockerfile docker
