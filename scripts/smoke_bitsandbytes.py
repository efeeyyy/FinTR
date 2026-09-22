"""Verify the local bitsandbytes CUDA and NF4 inference backend."""

from __future__ import annotations

import gc

import bitsandbytes as bnb
import torch
from bitsandbytes.cextension import lib


EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 4070 Laptop GPU"
EXPECTED_CAPABILITY = (8, 9)


def main() -> int:
    """Run a small NF4 round trip without loading model weights."""

    print(f"bitsandbytes version: {bnb.__version__}")
    print(f"bitsandbytes native library: {type(lib).__name__}")

    compiled_with_cuda = bool(getattr(lib, "compiled_with_cuda", False))
    print(f"bitsandbytes CUDA backend compiled: {compiled_with_cuda}")
    if not compiled_with_cuda:
        print("SMOKE_TEST: FAIL - bitsandbytes CUDA backend is unavailable")
        return 1

    cuda_available = torch.cuda.is_available()
    print(f"torch.cuda.is_available(): {cuda_available}")
    if not cuda_available or torch.cuda.device_count() < 1:
        print("SMOKE_TEST: FAIL - NVIDIA CUDA device is unavailable")
        return 1

    device = torch.device("cuda:0")
    gpu_name = torch.cuda.get_device_name(device)
    capability = torch.cuda.get_device_capability(device)
    print(f"GPU name: {gpu_name}")
    print(f"Compute capability: {capability[0]}.{capability[1]}")

    if gpu_name != EXPECTED_GPU_NAME:
        print(f"SMOKE_TEST: FAIL - unexpected GPU: {gpu_name}")
        return 1
    if capability != EXPECTED_CAPABILITY:
        print(f"SMOKE_TEST: FAIL - unexpected compute capability: {capability}")
        return 1

    source = torch.linspace(-1.0, 1.0, 4096, device=device, dtype=torch.bfloat16)
    quantized = None
    restored = None
    try:
        quantized, quant_state = bnb.functional.quantize_4bit(
            source,
            quant_type="nf4",
            compress_statistics=True,
        )
        restored = bnb.functional.dequantize_4bit(quantized, quant_state)
        torch.cuda.synchronize(device)

        nf4_ready = (
            quantized.device.type == "cuda"
            and restored.device.type == "cuda"
            and getattr(quant_state, "quant_type", None) == "nf4"
            and bool(getattr(quant_state, "nested", False))
            and bool(torch.isfinite(restored).all().item())
        )
        print(f"Quantized tensor device: {quantized.device}")
        print(f"Quantized storage dtype: {quantized.dtype}")
        print(f"Quantization type: {getattr(quant_state, 'quant_type', None)}")
        print(f"Double quantization/nested state: {getattr(quant_state, 'nested', None)}")
        print(f"NF4 backend operation: {'PASS' if nf4_ready else 'FAIL'}")
        if not nf4_ready:
            print("SMOKE_TEST: FAIL - NF4 round trip did not meet all checks")
            return 1
    except Exception as exc:
        print(f"SMOKE_TEST: FAIL - NF4 CUDA operation failed: {exc}")
        return 1
    finally:
        del source, quantized, restored
        gc.collect()
        torch.cuda.empty_cache()

    print("SMOKE_TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
