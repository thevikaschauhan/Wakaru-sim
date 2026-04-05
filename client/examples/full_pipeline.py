"""
MiroFish Full Pipeline Example
-------------------------------
Generic end-to-end demo: upload a text file, run the simulation, get a report.

Prerequisites:
    1. MiroFish backend running:  cd backend && python run.py
    2. pip install ../            (installs mirofish package)
    3. Set env vars: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME, ZEP_API_KEY

Run:
    python full_pipeline.py path/to/your/document.txt "Your prediction question here"
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mirofish import MiroFishClient


def on_progress(stage: str, state) -> None:
    status = getattr(state, "status", None) or getattr(state, "runner_status", None)
    progress = getattr(state, "progress_percent", None)
    msg = f"[{stage}]"
    if status:
        msg += f" status={status}"
    if progress is not None:
        msg += f" progress={progress:.0f}%"
    print(msg)


def main():
    if len(sys.argv) < 3:
        print("Usage: python full_pipeline.py <file_path> <requirement>")
        sys.exit(1)

    file_path = sys.argv[1]
    requirement = sys.argv[2]

    if not Path(file_path).exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    client = MiroFishClient("http://localhost:5001")

    print(f"\nRunning MiroFish pipeline")
    print(f"File       : {file_path}")
    print(f"Requirement: {requirement}\n")

    report = client.run_full_pipeline(
        files=[file_path],
        requirement=requirement,
        enable_twitter=True,
        enable_reddit=False,
        on_progress=on_progress,
    )

    print("\n" + "=" * 60)
    print("SIMULATION REPORT")
    print("=" * 60)
    print(report.content)

    # Save to file
    output_path = Path(file_path).stem + "_report.md"
    Path(output_path).write_text(report.content, encoding="utf-8")
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
