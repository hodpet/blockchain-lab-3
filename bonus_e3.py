import random
import numpy as np
import matplotlib.pyplot as plt

def simulate_selfish_mining(alpha: float, gamma: float, epochs: int = 200000) -> float:
    """Eyal-Sirer Selfish Mining Markov Chain Simulator."""
    honest_rev = 0
    selfish_rev = 0
    state = 0  # Represents the selfish miner's lead (0, 1, 2, 3...). -1 represents the 0' tie state.
    
    for _ in range(epochs):
        pool_mines_next = random.random() < alpha
        
        if state == 0:
            if pool_mines_next:
                state = 1
            else:
                honest_rev += 1
                
        elif state == 1:
            if pool_mines_next:
                state = 2
            else:
                state = -1  # Tie (0')
                
        elif state == -1: # Tie State
            if pool_mines_next:
                selfish_rev += 2
                state = 0
            else:
                # Honest network finds a block. Who did they build on?
                if random.random() < gamma:
                    selfish_rev += 1
                    honest_rev += 1
                else:
                    honest_rev += 2
                state = 0
                
        elif state == 2:
            if pool_mines_next:
                state = 3
            else:
                selfish_rev += 2
                state = 0
                
        elif state > 2:
            if pool_mines_next:
                state += 1
            else:
                # Honest finds a block, pool publishes 1 block to maintain lead
                selfish_rev += 1
                state -= 1
                
    return selfish_rev / (selfish_rev + honest_rev)

alphas = np.linspace(0, 0.5, 50)
gamma_0 = [simulate_selfish_mining(a, 0.0) for a in alphas]
gamma_05 = [simulate_selfish_mining(a, 0.5) for a in alphas]
honest_line = alphas

plt.figure(figsize=(8, 5))
plt.plot(alphas, gamma_0, label=r'$\gamma = 0$ (Poor network connectivity)', color='red')
plt.plot(alphas, gamma_05, label=r'$\gamma = 0.5$ (Average connectivity)', color='purple')
plt.plot(alphas, honest_line, label='Honest Mining (Revenue = $\\alpha$)', color='gray', linestyle='--')

plt.axvline(x=0.25, color='gray', linestyle=':', alpha=0.5)
plt.axvline(x=0.33, color='gray', linestyle=':', alpha=0.5)

plt.title("Selfish Mining Profitability (Eyal & Sirer)")
plt.xlabel("Attacker Hash Share ($\\alpha$)")
plt.ylabel("Attacker Revenue Share")
plt.legend()
plt.grid(True, linestyle="--", alpha=0.5)
plt.tight_layout()
plt.savefig("bonus_e3_selfish.png")
print("Saved 'bonus_e3_selfish.png'")
