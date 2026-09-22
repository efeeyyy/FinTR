"""Download, load, and benchmark one quantized local model response."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import perf_counter


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from fintr.inference.local import (  # noqa: E402
    LocalModelConfig,
    generate_once,
    load_quantized_model,
    select_compute_dtype,
)


DEFAULT_MODEL_ID = "Qwen/Qwen3-4B-Instruct-2507"
DEFAULT_PROMPT = (
    "Bir şirketin net kârı yıllık bazda %45 artmış ancak işletme faaliyetlerinden "
    "nakit akışı negatife dönmüş. Yalnızca bu bilgilere dayanarak finansal durumu "
    "değerlendir ve daha sağlıklı yorum yapabilmek için hangi ek verilere ihtiyaç "
    "duyduğunu belirt."
)


def directory_size(path: Path) -> int:
    """Return the total physical file size below a directory."""

    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def ensure_external_cache(path: Path) -> Path:
    """Reject model caches located inside the Git repository."""

    resolved = path.expanduser().resolve()
    if resolved == REPOSITORY_ROOT or resolved.is_relative_to(REPOSITORY_ROOT):
        raise ValueError(f"model cache must be outside the repository: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def parse_args() -> argparse.Namespace:
    """Parse smoke-test options."""

    default_cache = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "fintr-llm" / "huggingface"
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--cache-dir", type=Path, default=default_cache)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    return parser.parse_args()


def main() -> int:
    """Run the download and single-prompt inference baseline."""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args()
    cache_dir = ensure_external_cache(args.cache_dir)
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HF_HUB_CACHE"] = str(cache_dir / "hub")
    os.environ["HF_XET_CACHE"] = str(cache_dir / "xet")

    import torch
    from huggingface_hub import HfApi, snapshot_download

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")

    bf16_supported = torch.cuda.is_bf16_supported()
    compute_dtype = select_compute_dtype()
    print(f"BF16 supported: {bf16_supported}")
    print(f"Selected compute dtype: {compute_dtype}")
    print(f"External cache directory: {cache_dir}")

    api = HfApi()
    model_info = api.model_info(args.model_id, revision=args.revision, files_metadata=True)
    resolved_revision = model_info.sha
    repository_payload_bytes = sum(
        sibling.size or 0 for sibling in model_info.siblings if sibling.size is not None
    )
    print(f"Resolved revision: {resolved_revision}")

    cache_bytes_before = directory_size(cache_dir)
    download_started = perf_counter()
    snapshot_path = Path(
        snapshot_download(
            repo_id=args.model_id,
            revision=resolved_revision,
            cache_dir=cache_dir / "hub",
        )
    )
    download_seconds = perf_counter() - download_started
    cache_bytes_after = directory_size(cache_dir)
    cache_delta_bytes = max(0, cache_bytes_after - cache_bytes_before)

    quantization = {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "bnb_4bit_compute_dtype": str(compute_dtype),
        "device_map": {"": 0},
        "cpu_offload": False,
    }
    stage = "model_load"
    try:
        loaded = load_quantized_model(
            LocalModelConfig(
                model_id=str(snapshot_path),
                cache_dir=cache_dir / "hub",
            )
        )
        stage = "generation"
        result = generate_once(
            loaded,
            DEFAULT_PROMPT,
            max_new_tokens=args.max_new_tokens,
        )
    except (torch.OutOfMemoryError, RuntimeError) as exc:
        is_oom = isinstance(exc, torch.OutOfMemoryError) or "out of memory" in str(exc).lower()
        failure = {
            "status": "OOM" if is_oom else "ERROR",
            "stage": stage,
            "error": str(exc),
            "quantization_config": quantization,
            "peak_vram_mib": torch.cuda.max_memory_allocated(0) / 1024**2,
        }
        print("INFERENCE_FAILURE_JSON")
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2

    model = loaded.model
    is_4bit = bool(getattr(model, "is_loaded_in_4bit", False))
    device_map = getattr(model, "hf_device_map", None)
    parameter_devices = sorted({str(parameter.device) for parameter in model.parameters()})
    all_parameters_on_cuda = bool(parameter_devices) and all(
        device.startswith("cuda") for device in parameter_devices
    )
    metrics = {
        "status": "PASS",
        "model_id": args.model_id,
        "revision": resolved_revision,
        "snapshot_path": str(snapshot_path),
        "repository_payload_bytes": repository_payload_bytes,
        "cache_delta_bytes": cache_delta_bytes,
        "download_seconds": download_seconds,
        "load_seconds": loaded.load_seconds,
        "load_allocated_vram_mib": loaded.allocated_mib,
        "load_reserved_vram_mib": loaded.reserved_mib,
        "peak_vram_mib": result.peak_mib,
        "prompt_tokens": result.prompt_tokens,
        "generated_tokens": result.generated_tokens,
        "generation_seconds": result.generation_seconds,
        "tokens_per_second": result.tokens_per_second,
        "bf16_supported": bf16_supported,
        "compute_dtype": str(compute_dtype),
        "is_loaded_in_4bit": is_4bit,
        "device_map": device_map,
        "parameter_devices": parameter_devices,
        "all_parameters_on_cuda": all_parameters_on_cuda,
        "quantization_config": quantization,
    }
    print("INFERENCE_METRICS_JSON")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print("RAW_RESPONSE_BEGIN")
    print(result.text)
    print("RAW_RESPONSE_END")

    if not is_4bit or not all_parameters_on_cuda:
        print("SMOKE_TEST: FAIL - 4-bit CUDA-only placement was not verified")
        return 1
    print("SMOKE_TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
