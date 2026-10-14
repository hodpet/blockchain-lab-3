import numpy as np
import matplotlib.pyplot as plt
from consensus.pow import simulate_mining

# 1. Generate Data for Interval Histogram & Statistics
chain, stats = simulate_mining(blocks=100, workers=1, target_seconds=0.3)
intervals = stats.intervals

print(f"Mean interval: {np.mean(intervals):.4f}s")
print(f"Median / Mean: {np.median(intervals) / np.mean(intervals):.4f}")
print(f"StDev / Mean:  {np.std(intervals) / np.mean(intervals):.4f}")

# Plot Histogram of Intervals
plt.figure(figsize=(8, 5))
plt.hist(intervals, bins=15, edgecolor='black', alpha=0.7)
plt.title("Inter-Block Interval Distribution (100 Blocks, Workers=1)")
plt.xlabel("Interval (seconds)")
plt.ylabel("Frequency")
plt.grid(True, linestyle="--", alpha=0.5)
plt.tight_layout()
plt.savefig("part_a_histogram.png")
plt.close()

# 2. Generate Data for Difficulty Plot
chain_retarget, stats_retarget = simulate_mining(blocks=120, workers=1, target_seconds=1.0)
difficulties = stats_retarget.difficulties

# Plot Difficulty across Windows
plt.figure(figsize=(8, 5))
plt.plot(range(1, len(difficulties) + 1), difficulties, marker='o', linestyle='-')
plt.title("Difficulty Adjustment Across Retarget Windows (120 Blocks)")
plt.xlabel("Window Index")
plt.ylabel("Difficulty")
plt.grid(True, linestyle="--", alpha=0.5)
plt.tight_layout()
plt.savefig("part_a_difficulty.png")
plt.close()

print("\nPlots generated and saved as 'part_a_histogram.png' and 'part_a_difficulty.png'!")
