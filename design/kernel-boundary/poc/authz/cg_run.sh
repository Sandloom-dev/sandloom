#!/bin/bash
# One line of cg_jobs.txt: SIDE VARIANT FLAG|- CASE (run by xargs -P).
. "$(dirname "$0")/env.sh"
cd "$(dirname "$0")"
set -- $1
side=$1 variant=$2 flag=$3 case=$4
[ "$flag" != - ] && export "$flag"
SIDE=$side VARIANT=$variant ./cg_poc.sh "$case"
