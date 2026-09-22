"""Reusable local Transformers inference with bitsandbytes 4-bit loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


MIB = 1024**2


@dataclass(frozen=True)
class LocalModelConfig:
    """Configuration for a local, single-GPU quantized model."""

    model_id: str
    revision: str | None = None
    cache_dir: Path | None = None
    load_in_4bit: bool = True
    quant_type: str = "nf4"
    double_quant: bool = True


@dataclass
class LoadedLocalModel:
    """Loaded model, tokenizer, and measured load metadata."""

    model: Any
    tokenizer: Any
    compute_dtype: torch.dtype
    load_seconds: float
    allocated_mib: float
    reserved_mib: float
    peak_mib: float


@dataclass(frozen=True)
class GenerationResult:
    """Raw generated text and inference measurements."""

    text: str
    prompt_tokens: int
    generated_tokens: int
    generation_seconds: float
    tokens_per_second: float
    peak_mib: float


def select_compute_dtype() -> torch.dtype:
    """Prefer BF16 on supported CUDA hardware, otherwise use FP16."""

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for local quantized inference")
    return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16


def build_quantization_config(compute_dtype: torch.dtype) -> BitsAndBytesConfig:
    """Build the project's inference-only NF4 configuration."""

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )


def load_quantized_model(config: LocalModelConfig) -> LoadedLocalModel:
    """Load a model fully on CUDA device 0 without automatic CPU offload."""

    if not config.load_in_4bit:
        raise ValueError("This loader currently requires load_in_4bit=True")
    if config.quant_type != "nf4" or not config.double_quant:
        raise ValueError("This baseline requires NF4 with double quantization")

    compute_dtype = select_compute_dtype()
    quantization_config = build_quantization_config(compute_dtype)
    common_kwargs: dict[str, Any] = {}
    if config.revision is not None:
        common_kwargs["revision"] = config.revision
    if config.cache_dir is not None:
        common_kwargs["cache_dir"] = str(config.cache_dir)

    tokenizer = AutoTokenizer.from_pretrained(config.model_id, **common_kwargs)

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats(0)
    started = perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        config.model_id,
        quantization_config=quantization_config,
        device_map={"": 0},
        dtype=compute_dtype,
        low_cpu_mem_usage=True,
        **common_kwargs,
    )
    torch.cuda.synchronize(0)
    load_seconds = perf_counter() - started
    model.eval()

    return LoadedLocalModel(
        model=model,
        tokenizer=tokenizer,
        compute_dtype=compute_dtype,
        load_seconds=load_seconds,
        allocated_mib=torch.cuda.memory_allocated(0) / MIB,
        reserved_mib=torch.cuda.memory_reserved(0) / MIB,
        peak_mib=torch.cuda.max_memory_allocated(0) / MIB,
    )


def generate_once(
    loaded: LoadedLocalModel,
    prompt: str,
    *,
    max_new_tokens: int = 256,
) -> GenerationResult:
    """Generate one deterministic chat response without adding a system prompt."""

    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not 1 <= max_new_tokens <= 300:
        raise ValueError("max_new_tokens must be between 1 and 300 for this baseline")

    inputs = loaded.tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(loaded.model.device)
    prompt_tokens = int(inputs["input_ids"].shape[-1])

    torch.cuda.synchronize(0)
    started = perf_counter()
    with torch.inference_mode():
        output_ids = loaded.model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            pad_token_id=loaded.tokenizer.eos_token_id,
        )
    torch.cuda.synchronize(0)
    generation_seconds = perf_counter() - started

    new_ids = output_ids[0, prompt_tokens:]
    generated_tokens = int(new_ids.shape[-1])
    text = loaded.tokenizer.decode(new_ids, skip_special_tokens=True)
    tokens_per_second = generated_tokens / generation_seconds if generation_seconds else 0.0

    return GenerationResult(
        text=text,
        prompt_tokens=prompt_tokens,
        generated_tokens=generated_tokens,
        generation_seconds=generation_seconds,
        tokens_per_second=tokens_per_second,
        peak_mib=torch.cuda.max_memory_allocated(0) / MIB,
    )
