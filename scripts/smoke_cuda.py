"""Run a small, offline CUDA smoke test for the FinTR-LLM environment."""

from __future__ import annotations

import gc
import platform
import sys
import weakref


MIB = 1024**2


def mib(value: int) -> float:
    """Convert bytes to mebibytes."""

    return value / MIB


def main() -> int:
    """Report the environment and verify a small matrix product on CUDA."""

    left = None
    right = None
    result = None
    tensor_refs: list[weakref.ReferenceType[torch.Tensor]] = []
    try:
        import torch
    except Exception as exc:
        print(f"SMOKE_TEST: FAIL - torch import failed: {exc}")
        return 1

    print(f"Python version: {platform.python_version()}")
    print(f"Python executable: {sys.executable}")
    print(f"PyTorch version: {torch.__version__}")
    print(f"PyTorch CUDA runtime: {torch.version.cuda}")

    cuda_available = torch.cuda.is_available()
    print(f"torch.cuda.is_available(): {cuda_available}")
    if not cuda_available:
        print("SMOKE_TEST: FAIL - CUDA is not available to PyTorch")
        return 1

    device_count = torch.cuda.device_count()
    print(f"CUDA device count: {device_count}")
    if device_count < 1:
        print("SMOKE_TEST: FAIL - no CUDA device was found")
        return 1

    device = torch.device("cuda:0")
    properties = torch.cuda.get_device_properties(device)
    capability = torch.cuda.get_device_capability(device)
    free_before, total_memory = torch.cuda.mem_get_info(device)

    print(f"GPU name: {properties.name}")
    print(f"Compute capability: {capability[0]}.{capability[1]}")
    print(f"Total VRAM: {mib(total_memory):.2f} MiB")
    print(f"Initial free VRAM: {mib(free_before):.2f} MiB")

    try:
        left = torch.full((64, 64), 1.5, device=device, dtype=torch.float32)
        right = torch.full((64, 64), 0.5, device=device, dtype=torch.float32)
        result = left @ right
        torch.cuda.synchronize(device)

        if result.device.type != "cuda":
            raise RuntimeError(f"matrix result is on {result.device}, not CUDA")
        if result.shape != (64, 64):
            raise RuntimeError(f"unexpected result shape: {tuple(result.shape)}")
        if not bool(torch.isfinite(result).all().item()):
            raise RuntimeError("matrix result contains a non-finite value")

        print(f"Matrix multiplication device: {result.device}")
        print(f"Matrix multiplication shape: {tuple(result.shape)}")
        print("CUDA matrix multiplication: PASS")
        tensor_refs = [weakref.ref(left), weakref.ref(right), weakref.ref(result)]
    except Exception as exc:
        print(f"SMOKE_TEST: FAIL - CUDA tensor operation failed: {exc}")
        return 1
    finally:
        del left, right, result
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize(device)

    free_after, _ = torch.cuda.mem_get_info(device)
    allocated_after = torch.cuda.memory_allocated(device)
    reserved_after = torch.cuda.memory_reserved(device)
    tensors_released = all(reference() is None for reference in tensor_refs)
    print(f"Free VRAM after cleanup: {mib(free_after):.2f} MiB")
    print(f"PyTorch allocated VRAM after cleanup: {mib(allocated_after):.2f} MiB")
    print(f"PyTorch reserved VRAM after cleanup: {mib(reserved_after):.2f} MiB")
    print(f"Test tensor references released: {tensors_released}")
    if not tensors_released:
        print("SMOKE_TEST: FAIL - one or more test tensors survived cleanup")
        return 1
    print("CUDA memory cleanup: PASS")
    print("SMOKE_TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
