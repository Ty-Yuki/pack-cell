#!/bin/bash
# コンテナ内でセルを起動し、コントローラが揃ってから引数のコマンドを実行して終了する。
# usage: run_sim.sh [--seed N] [--gui] -- <cmd...>
# 並列実行: RUN_ID を変えると GZ_PARTITION / ROS_DOMAIN_ID / ビルド先 / ログ先が分離される。
set -euo pipefail
cd "$(dirname "$0")/.."
SEED=0; GUI=false
while [ $# -gt 0 ]; do
  case "$1" in
    --seed) SEED=$2; shift 2 ;;
    --gui) GUI=true; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done
CMD="$*"
ID=${RUN_ID:-0}
mkdir -p logs
GZ_PARTITION=run$ID ROS_DOMAIN_ID=$((ID + 30)) ./scripts/docker_run.sh bash -lc "
export SIM_BUILD=/ws/sim/build_$ID
source /opt/ros/jazzy/setup.bash
python3 sim/gen_world.py \$SIM_BUILD --seed $SEED ${GEN_ARGS:-} >/dev/null || { echo "GEN_WORLD_FAILED"; exit 9; }
(ros2 launch /ws/sim/launch/cell.launch.py gui:=$GUI > /ws/logs/sim_$ID.log 2>&1 &)
for i in \$(seq 1 90); do [ \$(ros2 control list_controllers -c /arm_a/controller_manager 2>/dev/null | grep -c active) -ge 2 ] && [ \$(ros2 control list_controllers -c /arm_b/controller_manager 2>/dev/null | grep -c active) -ge 2 ] && break; sleep 1; done
sleep 2
$CMD
rc=\$?
pkill -f 'gz sim' || true; pkill -f ros2 || true
exit \$rc"
