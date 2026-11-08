#!/bin/sh
# kb-poc-trust T-15 micro rung under cachegrind: KERNEL=<worktree> TAG=x ./cg_micro.sh [K]
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
K=${1:-20000}
IMAGE=${CG_IMAGE:-sandloom-ladder-cg:3.13}
for N in 0 "$K"; do
  IR=$(docker run --rm -e PYTHONHASHSEED=0 -e PYTHONPATH=/w/src \
    -v "$KERNEL/src:/w/src:ro" -v "$HERE:/w/bench:ro" -w /tmp "$IMAGE" \
    valgrind --tool=cachegrind --cache-sim=no --cachegrind-out-file=/dev/null \
    python /w/bench/micro_view.py "$N" 2>&1 | sed -n 's/.*I *refs: *\([0-9,]*\).*/\1/p' | tr -d ,)
  eval "IR_$N=\$IR"
done
eval "A=\$IR_$K"
echo "$TAG micro $(( (A - IR_0) / K )) Ir/op (K=$K, Ir0=$IR_0, IrK=$A)"
