#!/usr/bin/env bash
# Synthetic background activity for testing the audio path: every 2 s two short
# CPU-bound Python processes and a handful of process spawns, on the CPUs given
# ("0-3" by default). Usage: aec-test-cpu-load.sh [CPUS] [SECONDS]
# Run it next to aec-ref-timing-probe.py; see that script for PROBE_PIN_CPUS.
CPUS=${1:-0-3}; SECS=${2:-170}
end=$((SECONDS + SECS)); n=0
sleep 10
while [ $SECONDS -lt $end ]; do
  n=$((n + 1))
  taskset -c $CPUS python3 -c 'sum(i*i for i in range(2500000))' &
  taskset -c $CPUS python3 -c 'import json,re,subprocess,threading; sum(i*i for i in range(1500000))' &
  for i in 1 2 3 4 5 6; do taskset -c $CPUS /bin/ls -R /usr/lib/python3 >/dev/null 2>&1; done
  wait
  sleep 2
done
echo "load: $n rounds on CPUs $CPUS"
