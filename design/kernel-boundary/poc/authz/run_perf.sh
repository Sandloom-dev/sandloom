#!/bin/bash
# rusage instructions and latency, base vs candidate alternating, mono and
# split (kb-poc-authz Z-6). Output: results/run_<variant>_<topology>_<rep>.json
. "$(dirname "$0")/env.sh"
cd "$(dirname "$0")"
CASES=control,bound.control,orgrole,subactive,bound.orgrole_and_subactive
for rep in 1 2; do
  for t in mono split; do
    for sv in "base authzstand" "wt authzpa2"; do
      set -- $sv
      stand $1 $2 $t run --n ${N:-1000} --reps 3 --warmup 100 --cases $CASES \
        --out results/run_${2}_${t}_$rep.json >/dev/null 2>&1 || echo "FAILED $1 $2 $t $rep"
    done
  done
done
# PA-3 and the separate changes, split, decisive shape only
stand wt authzpa3 split run --n ${N:-1000} --reps 3 --warmup 100 \
  --cases bound.orgrole_and_subactive --out results/run_authzpa3_split_1.json >/dev/null 2>&1
KB_POC_K6=1 stand wt authzpa2 split run --n ${N:-1000} --reps 3 --warmup 100 \
  --cases bound.orgrole_and_subactive,bound.control --out results/run_authzpa2k6_split_1.json >/dev/null 2>&1
KB_POC_WITNESS=1 stand wt authzpa2 split run --n ${N:-1000} --reps 3 --warmup 100 \
  --cases bound.orgrole_and_subactive --out results/run_authzpa2witness_split_1.json >/dev/null 2>&1
echo done
