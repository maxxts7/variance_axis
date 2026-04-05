"""Launch the generation experiment across 2 GPUs via data parallelism.

Splits prompts in half, runs one process per GPU, merges results.

Usage:
    python run_parallel.py --preset thorough          # 2 GPUs (default)
    python run_parallel.py --preset full --gpus 0 1   # explicit GPU IDs
    python run_parallel.py --preset full --gpus 0 1 2 3  # 4 GPUs
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="Run generation experiment in parallel across GPUs"
    )
    parser.add_argument(
        "--preset", choices=["sanity", "thorough", "full"], default="full",
        help="Configuration preset",
    )
    parser.add_argument(
        "--gpus", type=int, nargs="+", default=[0, 1],
        help="GPU IDs to use (default: 0 1)",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Final merged output directory (default: preset's default)",
    )
    args = parser.parse_args()

    n_gpus = len(args.gpus)
    preset = args.preset

    # Determine prompt count per preset to split evenly
    prompt_counts = {"sanity": 5, "thorough": 15, "full": 50}
    n_prompts = prompt_counts[preset]

    # Split prompts across GPUs as evenly as possible
    chunk_size = n_prompts // n_gpus
    remainder = n_prompts % n_gpus
    slices = []
    start = 0
    for i in range(n_gpus):
        end = start + chunk_size + (1 if i < remainder else 0)
        slices.append((start, end))
        start = end

    # Temp output dirs per GPU
    tmp_dirs = [Path(f"_parallel_tmp/gpu{gpu}") for gpu in args.gpus]
    for d in tmp_dirs:
        d.mkdir(parents=True, exist_ok=True)

    print(f"Preset: {preset}")
    print(f"GPUs: {args.gpus}")
    print(f"Prompt splits: {['{}:{}'.format(s, e) for s, e in slices]}")
    print(f"Temp dirs: {[str(d) for d in tmp_dirs]}")
    print()

    # Launch one process per GPU
    t0 = time.time()
    procs = []
    for i, (gpu, (s, e), tmp_dir) in enumerate(zip(args.gpus, slices, tmp_dirs)):
        cmd = [
            sys.executable, "run_generation.py",
            "--preset", preset,
            "--gpu", str(gpu),
            "--prompt-slice", f"{s}:{e}",
            "--output-dir", str(tmp_dir),
        ]
        print(f"[GPU {gpu}] Launching: prompts [{s}:{e}] ({e - s} prompts)")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append((gpu, proc))

    # Wait for all processes, streaming output
    print(f"\nWaiting for {n_gpus} processes...\n")
    exit_codes = {}
    for gpu, proc in procs:
        stdout, _ = proc.communicate()
        exit_codes[gpu] = proc.returncode
        # Print last few lines of output per GPU
        lines = stdout.decode(errors="replace").strip().split("\n")
        tail = lines[-5:] if len(lines) > 5 else lines
        print(f"--- GPU {gpu} (exit {proc.returncode}) ---")
        for line in tail:
            print(f"  {line}")
        print()

    elapsed = time.time() - t0

    # Check for failures
    failed = [gpu for gpu, code in exit_codes.items() if code != 0]
    if failed:
        print(f"ERROR: GPUs {failed} failed. Check output above.")
        sys.exit(1)

    # Merge results
    output_defaults = {
        "sanity": "results/generation_sanity",
        "thorough": "results/generation_thorough",
        "full": "results/generation",
    }
    final_dir = Path(args.output_dir if args.output_dir else output_defaults[preset])
    final_dir.mkdir(parents=True, exist_ok=True)

    print(f"Merging results into {final_dir}/")
    for fname in ["generations.csv", "per_step_metrics.csv"]:
        parts = []
        for tmp_dir in tmp_dirs:
            csv_path = tmp_dir / fname
            if csv_path.exists():
                parts.append(pd.read_csv(csv_path))
            else:
                print(f"  WARNING: {csv_path} not found")
        if parts:
            merged = pd.concat(parts, ignore_index=True)
            sort_cols = ["prompt_idx"]
            if "direction_type" in merged.columns:
                sort_cols += ["direction_type", "alpha", "perturb_mode"]
            if "step" in merged.columns:
                sort_cols.append("step")
            merged.sort_values([c for c in sort_cols if c in merged.columns], inplace=True)
            merged.to_csv(final_dir / fname, index=False)
            print(f"  {fname}: {len(merged)} rows")

    # Copy version.json from first GPU's output
    version_src = tmp_dirs[0] / "version.json"
    if version_src.exists():
        import shutil
        shutil.copy2(version_src, final_dir / "version.json")
        print(f"  version.json copied")

    # Cleanup temp dirs
    import shutil
    shutil.rmtree("_parallel_tmp", ignore_errors=True)
    print(f"  Cleaned up temp dirs")

    print(f"\nDone in {elapsed / 60:.1f} minutes ({n_gpus} GPUs).")
    print(f"Results: {final_dir}/")


if __name__ == "__main__":
    main()
