import matplotlib.pyplot as plt
from consensus.pos import BeaconState, Attestation, Checkpoint, GWEI

offenders_list = [1, 5, 10, 20, 30]
losses = []

print(f"{'Offenders':<12} | {'Loss (ETH)':<10}")
print("-" * 25)

for k in offenders_list:
    state = BeaconState(validator_count=64)
    
    # Define our checkpoints
    genesis = state.genesis_checkpoint
    target_A = Checkpoint(1, "root-A")
    target_B = Checkpoint(1, "root-B")
    
    # All 64 validators attest to genesis -> chain-A
    for i in range(64):
        state.attest(Attestation(validator=i, source=genesis, target=target_A))
        
    # The first k validators also attest to genesis -> chain-B (equivocation/double vote)
    for i in range(k):
        state.attest(Attestation(validator=i, source=genesis, target=target_B))
        
    # Process epoch 1 to trigger the slashing penalties
    state.process_epoch(1)
    
    # Initial balance is 32 ETH. We check the balance of the first validator (index 0)
    loss = 32.0 - (state.validators[0].balance / GWEI)
    losses.append(loss)
    
    print(f"{k:<12} | {loss:.4f}")

# Plotting
plt.figure(figsize=(8, 5))
plt.plot(offenders_list, losses, marker='o', linestyle='-', color='red')
plt.axhline(y=32, color='gray', linestyle='--', alpha=0.7, label='Max Balance (32 ETH)')
plt.title("Correlated Penalty: Loss per Validator vs. Simultaneous Offenders")
plt.xlabel("Number of Simultaneous Offenders")
plt.ylabel("Loss per Validator (ETH)")
plt.legend()
plt.grid(True, linestyle="--", alpha=0.5)
plt.tight_layout()
plt.savefig("part_b_penalty.png")
plt.close()

print("\nPlot saved as 'part_b_penalty.png'!")
