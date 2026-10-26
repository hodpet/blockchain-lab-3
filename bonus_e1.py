import math
import random

def formula_probability(q: float, z: int) -> float:
    p = 1.0 - q
    lam = z * (q / p)
    sum_prob = 0.0
    for k in range(z + 1):
        # Use logarithms to prevent OverflowError for very large z and k values
        log_poisson = -lam + k * math.log(lam) - math.lgamma(k + 1)
        poisson = math.exp(log_poisson)
        
        prob = poisson * (1.0 - (q / p) ** (z - k))
        sum_prob += prob
    return 1.0 - sum_prob

def monte_carlo_sim(q: float, z: int, trials: int = 100000) -> float:
    successes = 0
    for _ in range(trials):
        honest = 0
        attacker = 0
        
        # Phase 1: Wait for honest network to find z blocks
        while honest < z:
            if random.random() < q:
                attacker += 1
            else:
                honest += 1
                
        # Phase 2: Attacker catch-up random walk
        deficit = honest - attacker
        if deficit <= 0:
            successes += 1
            continue
            
        while deficit > 0 and deficit < 100:  # 100 is a safe abort threshold 
            if random.random() < q:
                deficit -= 1
            else:
                deficit += 1
        
        if deficit <= 0:
            successes += 1
            
    return successes / trials

print("--- Whitepaper Probability Table ---")
for q, z in [(0.1, 5), (0.1, 6), (0.3, 5)]:
    p_form = formula_probability(q, z)
    p_mc = monte_carlo_sim(q, z)
    print(f"q={q}, z={z}  -> Formula: {p_form:.7f} | Monte Carlo: {p_mc:.7f}")

print("\n--- Confirmations needed for P < 0.001 ---")
for q in [0.1, 0.3, 0.45]:
    z = 1
    while formula_probability(q, z) >= 0.001:
        z += 1
    print(f"q={q:.2f} -> z={z}")
