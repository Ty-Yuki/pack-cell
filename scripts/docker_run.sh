#!/bin/bash
# リポジトリを /ws にマウントして起動する。ホストのローカルLLM (:30000) は --network host でそのまま見える。
# 画面なし(tty/ssh)でも動くよう、GUI は DISPLAY があるときだけ渡す。
set -euo pipefail
cd "$(dirname "$0")/.."
ARGS=()
if [ -n "${DISPLAY:-}" ]; then
  ARGS+=(-e DISPLAY="$DISPLAY" -v /tmp/.X11-unix:/tmp/.X11-unix)
fi
[ -t 0 ] && ARGS+=(-it)
exec docker run --rm --gpus all --network host -e PACK_DEBUG="${PACK_DEBUG:-}" -e PACK_LAZY_JOINTS="${PACK_LAZY_JOINTS:-}" -e HANG_DUMP_SEC="${HANG_DUMP_SEC:-}" \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:30000/v1}" -e LLM_MODE="${LLM_MODE:-record}" -e LLM_MODEL="${LLM_MODEL:-}" \
  -e GZ_PARTITION="${GZ_PARTITION:-pack}" -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}" \
  "${ARGS[@]}" \
  -v "$PWD":/ws -w /ws \
  "${PACK_IMAGE:-pack-cell:jazzy}" "${@:-bash}"
