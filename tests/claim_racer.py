"""Race jobs.claim_delivery from several threads (test_lifecycle's stress test).

Usage: claim_racer.py THREADS SLOT_SECONDS JOB_ID...

Prints "ready", reads the shared start time from stdin, then every thread
claims each job at start + index * slot, so all racers hit a job together.
Prints the job ids this process won as a JSON list (one entry per win).
"""

import json
import sys
import threading
import time

from agents_router import jobs


def race(job_ids: list[str], threads: int, start: float, slot: float) -> list[str]:
    wins: list[str] = []

    def claim_all() -> None:
        for index, job_id in enumerate(job_ids):
            delay = start + index * slot - time.time()
            if delay > 0:
                time.sleep(delay)
            if jobs.claim_delivery(job_id):
                wins.append(job_id)

    workers = [threading.Thread(target=claim_all) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return wins


def main() -> int:
    threads, slot, job_ids = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3:]
    print("ready", flush=True)
    start = float(sys.stdin.readline())
    print(json.dumps(race(job_ids, threads, start, slot)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
