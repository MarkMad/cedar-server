import torch

# Work around an illegal-instruction (SIGILL) crash in oneDNN/MKLDNN on
# Snapdragon/ARM64 CPUs (torch 2.8). Disabling MKLDNN makes ConvTranspose1d
# and other ops fall back to the plain CPU implementation.
torch.backends.mkldnn.enabled = False
try:
    torch.backends.openmp.enabled = False
except Exception:
    pass
