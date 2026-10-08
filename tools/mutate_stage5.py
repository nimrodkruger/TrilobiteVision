"""Mutation checks for the Stage 5 invariants.

Not "mutate everything". The implementation plan is explicit that blanket
mutation testing is not adopted, because equivalent mutations and incidental
changes dominate the maintenance cost. What it keeps mutation checking for is
the handful of invariants whose violation is silent, and this file is that list
for recording:

  * the drop accounting -- every exposed frame in the output or in a named gap;
  * the three-outcome `offer` contract, which is what makes the two independent
    frame counts comparable;
  * the start barrier, which is what stops the preview cap eating a frame that
    nothing then counts;
  * geometry and depth not changing mid-recording;
  * storage identity, the free-space reserve and the drop ceiling.

Each mutation is a plausible edit -- a flipped comparison, a dropped
increment, a removed check -- not random noise. A mutation that SURVIVES means
the test suite asserts the behaviour nowhere, and the test is missing.

Usage:

    .venv/bin/python tools/mutate_stage5.py            # all of them
    .venv/bin/python tools/mutate_stage5.py --only 3 7

Mutations are applied to the working tree and reverted in a `finally`, so an
interrupted run restores the file it was holding. Nothing is committed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"


@dataclass
class Mutation:
    what: str                 # the invariant it attacks
    path: str
    old: str
    new: str
    tests: tuple[str, ...]    # what should catch it


REC = "tests/test_recording.py"
ROUND = "tests/test_recording_roundtrip.py"
DEV = "tests/test_storage_devices.py"

MUTATIONS: list[Mutation] = [
    # -- the three-outcome offer contract --------------------------------
    Mutation(
        "a frame that arrives after the recording ended is counted as dropped",
        "src/trilobite/recording/continuous.py",
        "        if head is None or self.state != RECORDING:\n            return None",
        "        if head is None or self.state != RECORDING:\n            return False",
        (REC, ROUND),
    ),
    Mutation(
        "the same, for a burst",
        "src/trilobite/recording/burst.py",
        "        if buf is None or self.state != RECORDING:\n            return None",
        "        if buf is None or self.state != RECORDING:\n            return False",
        (REC,),
    ),
    # -- the drop accounting ---------------------------------------------
    Mutation(
        "a dropped frame is released but not counted",
        "src/trilobite/recording/continuous.py",
        "        if index is None:\n            self.dropped += 1\n"
        "            self._recent.append(False)",
        "        if index is None:\n            self.dropped += 0\n"
        "            self._recent.append(False)",
        (REC, ROUND),
    ),
    Mutation(
        "`exposed` counts stored frames rather than offered ones",
        "src/trilobite/recording/continuous.py",
        "        self.exposed += 1\n        if self.first_seq is None:",
        "        self.exposed += 0\n        if self.first_seq is None:",
        (REC, ROUND),
    ),
    # -- the start barrier ------------------------------------------------
    Mutation(
        "Start returns before the heads are recording",
        "src/trilobite/recording/manager.py",
        "                while not expected.issubset(self._entered):",
        "                while False and not expected.issubset(self._entered):",
        (ROUND,),
    ),
    Mutation(
        "a head with no frames is treated as started",
        "src/trilobite/recording/manager.py",
        "                missing = tuple(sorted(expected - self._entered))",
        "                missing = ()",
        (REC, ROUND),
    ),
    # -- geometry and depth do not change mid-recording -------------------
    Mutation(
        "a frame of the wrong shape is written into the chunk",
        "src/trilobite/recording/chunks.py",
        "        if pixels.shape != self.shape:",
        "        if False and pixels.shape != self.shape:",
        (REC,),
    ),
    Mutation(
        "a frame of the wrong dtype is written into the chunk",
        "src/trilobite/recording/chunks.py",
        "        if pixels.dtype != self.dtype:",
        "        if False and pixels.dtype != self.dtype:",
        (REC,),
    ),
    # -- the index is the authority ---------------------------------------
    Mutation(
        "a short final chunk keeps the full frame count in its header",
        "src/trilobite/recording/chunks.py",
        "            if rec.frames != self.chunk_frames:",
        "            if False and rec.frames != self.chunk_frames:",
        (ROUND,),
    ),
    # -- storage identity and the reserve ---------------------------------
    Mutation(
        "a different volume mounted at the same path is accepted",
        "src/trilobite/storage/identity.py",
        "    if dev != identity.st_dev:",
        "    if False and dev != identity.st_dev:",
        (DEV,),
    ),
    Mutation(
        "the free-space reserve is not enforced on admission",
        "src/trilobite/storage/writer.py",
        "        if free - need_bytes < reserve:",
        "        if False and free - need_bytes < reserve:",
        (DEV, REC),
    ),
    Mutation(
        "a quarantined device can be selected again without force",
        "src/trilobite/storage/writer.py",
        "        if not force and key in self._quarantine:",
        "        if False and not force and key in self._quarantine:",
        (DEV,),
    ),
    # -- the drop ceiling --------------------------------------------------
    Mutation(
        "the drop ceiling never fires",
        "src/trilobite/recording/continuous.py",
        "                    head.drop_fraction > self.max_drop_fraction):",
        "                    head.drop_fraction > self.max_drop_fraction and False):",
        (REC,),
    ),
    # -- an unsaved burst is never overwritten -----------------------------
    Mutation(
        "arming is allowed over an unsaved burst",
        "src/trilobite/recording/manager.py",
        "        if self.burst.unsaved:",
        "        if False and self.burst.unsaved:",
        (REC,),
    ),
]


def run(mutation: Mutation) -> tuple[bool, str]:
    path = ROOT / mutation.path
    original = path.read_text(encoding="utf-8")
    if mutation.old not in original:
        return False, "the text to mutate is not in the file (it moved)"
    if original.count(mutation.old) > 1:
        return False, "the text to mutate appears more than once (ambiguous)"
    try:
        path.write_text(original.replace(mutation.old, mutation.new),
                        encoding="utf-8")
        proc = subprocess.run(
            [str(PY), "-m", "pytest", "-x", "-q", "--no-header",
             *mutation.tests],
            cwd=ROOT, capture_output=True, text=True, timeout=900,
        )
    finally:
        path.write_text(original, encoding="utf-8")
    if proc.returncode != 0:
        tail = [ln for ln in proc.stdout.splitlines() if "FAILED" in ln or "failed" in ln]
        return True, (tail[-1].strip() if tail else "non-zero exit")
    return False, "the suite still passed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", type=int, nargs="*", help="1-based indices")
    args = ap.parse_args()

    chosen = list(enumerate(MUTATIONS, start=1))
    if args.only:
        wanted = set(args.only)
        chosen = [(i, m) for i, m in chosen if i in wanted]

    survivors = []
    for i, mutation in chosen:
        caught, detail = run(mutation)
        mark = "caught " if caught else "SURVIVED"
        print(f"{i:2d}. {mark}  {mutation.what}")
        print(f"              {detail}")
        if not caught:
            survivors.append((i, mutation))

    print()
    print(f"{len(chosen) - len(survivors)}/{len(chosen)} caught")
    for i, mutation in survivors:
        print(f"  SURVIVOR {i}: {mutation.what} ({mutation.path})")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
