import time
import multiprocessing as mp
import hashlib
import numpy as np
import matplotlib.pyplot as plt

def persistent_worker(task_queue, result_queue):
    """Worker process that stays alive and waits for mining tasks."""
    while True:
        task = task_queue.get()
        if task is None:  # Poison pill to shutdown
            break
            
        header, target_int, start_nonce, stride = task
        nonce = start_nonce
        
        while result_queue.empty():
            data = header + nonce.to_bytes(4, 'big')
            if int.from_bytes(hashlib.sha256(data).digest(), 'big') <= target_int:
                result_queue.put(nonce)
                break
            nonce += stride

def run_queue_miner(workers: int, blocks: int = 50, target_diff: int = 2**240):
    task_queue = mp.Queue()
    result_queue = mp.Queue()
    
    processes = []
    for _ in range(workers):
        p = mp.Process(target=persistent_worker, args=(task_queue, result_queue))
        p.start()
        processes.append(p)

    intervals = []
    start_time = time.time()
    
    for i in range(blocks):
        block_start = time.time()
        header = b'block' + i.to_bytes(4, 'big')
        
        # Dispatch work
        for w in range(workers):
            task_queue.put((header, target_diff, w, workers))
            
        # Wait for a winner
        winning_nonce = result_queue.get()
        
        # Clear result queue for next block
        while not result_queue.empty():
            result_queue.get()
            
        intervals.append(time.time() - block_start)

    total_time = time.time() - start_time
    
    # Shutdown workers
    for _ in range(workers):
        task_queue.put(None)
    for p in processes:
        p.join()
        
    return total_time, intervals

if __name__ == '__main__':
    print("Benchmarking reusable worker queues...")
    times = []
    worker_counts = [1, 2, 4]
    
    for w in worker_counts:
        print(f"\nRunning {w} workers (50 blocks)...")
        t_time, intervals = run_queue_miner(w, blocks=50)
        times.append(np.mean(intervals))
        
        if w == 4:
            print(f"4-Worker Stats -> Median/Mean: {np.median(intervals)/np.mean(intervals):.3f} | StDev/Mean: {np.std(intervals)/np.mean(intervals):.3f}")

    # Speed-up Plot
    plt.figure(figsize=(7, 4))
    plt.plot(worker_counts, times, marker='o', color='blue')
    plt.title("Scaling Efficiency of Reusable Queue Miners")
    plt.xlabel("Number of Workers")
    plt.ylabel("Mean Block Time (seconds)")
    plt.xticks(worker_counts)
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.savefig("bonus_e2_scaling.png")
    print("\nSaved 'bonus_e2_scaling.png'")
