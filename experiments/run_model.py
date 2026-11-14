"""Benchmark and latency test runner for Gemma-4-e4b model.

Supports two backends:
1. Local Hugging Face Transformers (`--backend transformers`)
2. Local or Remote vLLM / OpenAI-compatible endpoint (`--backend vllm` or `--api-base http://127.0.0.1:8000/v1`)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import List, Dict, Any



DEFAULT_MODEL_ID = "google/gemma-4-e4b-it-qat-w4a16-ct"

GENERIC_QUERIES = [
    "Write a Python function to check if a string is a valid IPv4 address without using any third-party libraries.",
    "Explain the difference between shallow copy and deep copy in Python with a brief code example.",
    "In git, what does `git add -N .` do and why is it useful before checking `git diff` in automated evaluation harnesses?",
]


def run_via_vllm_api(
    api_base: str,
    model_id: str,
    prompts: List[str],
    max_tokens: int = 256,
    temperature: float = 0.2,
) -> None:
    """Queries a running vLLM or OpenAI-compatible server on /v1/chat/completions."""
    endpoint = api_base.rstrip("/") + "/chat/completions"
    print(f"\n[vLLM API Mode] Connecting to {endpoint}")
    print(f"Model: {model_id}\n" + "=" * 60)

    total_time = 0.0
    total_tokens = 0

    for idx, prompt in enumerate(prompts, start=1):
        print(f"\n--- Query {idx}/{len(prompts)} ---")
        print(f"User: {prompt}\n")

        payload = {
            "model": model_id,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        auth_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        req = urllib.request.Request(
            endpoint,
            data=data,
            headers=headers,
        )

        start_t = time.perf_counter()
        try:
            with urllib.request.urlopen(req) as resp:
                elapsed = time.perf_counter() - start_t
                res_json = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[Error] Failed to connect or query {endpoint}: {e}")
            print("Ensure vLLM server is running or switch to '--backend transformers'.")
            return

        choice = res_json.get("choices", [{}])[0]
        msg = choice.get("message", {}).get("content", "").strip()
        usage = res_json.get("usage", {})
        completion_tokens = usage.get("completion_tokens", len(msg.split()))
        prompt_tokens = usage.get("prompt_tokens", 0)

        tps = completion_tokens / elapsed if elapsed > 0 else 0.0
        total_time += elapsed
        total_tokens += completion_tokens

        print(f"Assistant:\n{msg}\n")
        print(f"Latency: {elapsed:.3f} s | Tokens: {completion_tokens} (prompt: {prompt_tokens}) | Speed: {tps:.2f} tok/s")
        print("-" * 60)

    avg_speed = total_tokens / total_time if total_time > 0 else 0.0
    print("\n" + "=" * 60)
    print(f"Summary: {len(prompts)} queries completed in {total_time:.3f} s")
    print(f"Total Output Tokens: {total_tokens} | Average Speed: {avg_speed:.2f} tok/s")
    print("=" * 60)


def run_via_transformers(
    model_id: str,
    prompts: List[str],
    max_tokens: int = 256,
    device_arg: str = "auto",
    temperature: float = 0.2,
) -> None:
    """Loads and runs the model locally using Hugging Face Transformers."""
    try:
        import torch
        import transformers
        from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig
    except ImportError:
        print("[Error] PyTorch or Transformers not installed in current environment.")
        print("Please install them with:")
        print("  pip install torch transformers accelerate")
        sys.exit(1)

    print(f"\n[Transformers Mode] Loading model: {model_id}")
    
    # Select compute device
    if device_arg == "auto":
        if torch.cuda.is_available():
            device = "cuda"
            dtype = torch.bfloat16
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
            dtype = torch.float16
        else:
            device = "cpu"
            dtype = torch.float32
    else:
        device = device_arg
        dtype = torch.bfloat16 if device == "cuda" else torch.float32

    print(f"Selected Device: {device} | Dtype: {dtype}")
    
    load_start = time.perf_counter()
    try:
        import traceback
        config = AutoConfig.from_pretrained(model_id)
        arch = config.architectures[0] if config.architectures else "AutoModelForCausalLM"
        model_cls = getattr(transformers, arch, AutoModelForCausalLM)
        print(f"Detected Architecture: {arch} -> Using {model_cls.__name__}")

        tokenizer = AutoTokenizer.from_pretrained(model_id)
        
        # Load model weights
        load_kwargs = {"torch_dtype": dtype}
        if device == "cuda":
            load_kwargs["device_map"] = "auto"
        elif device == "mps":
            # For MPS, device_map='auto' is supported in recent accelerate or fallback to .to('mps')
            try:
                load_kwargs["device_map"] = "auto"
            except Exception:
                pass

        model = model_cls.from_pretrained(model_id, **load_kwargs)
        if device == "cpu" or (device == "mps" and not hasattr(model, "hf_device_map")):
            model.to(device)
    except Exception as e:
        import traceback
        print(f"[Error] Failed to load model '{model_id}': {e}")
        traceback.print_exc()
        print("\nNote: For compressed-tensors quantized models (*-qat-w4a16-ct):")
        print("1. Ensure `pip install compressed-tensors>=0.15.0` is installed.")
        print("2. Alternatively run with '--backend vllm' pointing to an active vLLM instance.")
        sys.exit(1)

    load_time = time.perf_counter() - load_start
    print(f"Model loaded successfully in {load_time:.2f} s\n" + "=" * 60)

    total_time = 0.0
    total_tokens = 0

    target_device = getattr(model, "device", device)

    for idx, prompt in enumerate(prompts, start=1):
        print(f"\n--- Query {idx}/{len(prompts)} ---")
        print(f"User: {prompt}\n")

        # Format input using chat template
        messages = [{"role": "user", "content": prompt}]
        if hasattr(tokenizer, "apply_chat_template"):
            encoded = tokenizer.apply_chat_template(
                messages,
                add_generation_prompt=True,
                return_tensors="pt",
            )
            if isinstance(encoded, dict) or hasattr(encoded, "items"):
                inputs = {k: v.to(target_device) for k, v in encoded.items()}
                prompt_len = inputs["input_ids"].shape[-1]
            else:
                inputs = {"input_ids": encoded.to(target_device)}
                prompt_len = encoded.shape[-1]
        else:
            tok_out = tokenizer(prompt, return_tensors="pt")
            inputs = {k: v.to(target_device) for k, v in tok_out.items()}
            prompt_len = inputs["input_ids"].shape[-1]

        start_t = time.perf_counter()

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=temperature if temperature > 0 else None,
                do_sample=temperature > 0,
                pad_token_id=tokenizer.eos_token_id or tokenizer.pad_token_id,
            )

        elapsed = time.perf_counter() - start_t
        gen_tokens = outputs[0][prompt_len:]
        completion_text = tokenizer.decode(gen_tokens, skip_special_tokens=True).strip()
        num_gen = len(gen_tokens)

        tps = num_gen / elapsed if elapsed > 0 else 0.0
        total_time += elapsed
        total_tokens += num_gen

        print(f"Assistant:\n{completion_text}\n")
        print(f"Latency: {elapsed:.3f} s | Tokens: {num_gen} (prompt: {prompt_len}) | Speed: {tps:.2f} tok/s")
        print("-" * 60)

    avg_speed = total_tokens / total_time if total_time > 0 else 0.0
    print("\n" + "=" * 60)
    print(f"Summary: {len(prompts)} queries completed in {total_time:.3f} s")
    print(f"Total Output Tokens: {total_tokens} | Average Speed: {avg_speed:.2f} tok/s")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run benchmark & latency test on Gemma-4 model.")
    parser.add_argument(
        "--model-id",
        type=str,
        default=DEFAULT_MODEL_ID,
        help=f"Hugging Face model ID or path (default: {DEFAULT_MODEL_ID})",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["transformers", "vllm"],
        default="transformers",
        help="Inference backend: 'transformers' (local HF) or 'vllm' (HTTP API)",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
        help="API base URL for vLLM / OpenAI server (default: http://127.0.0.1:8000/v1)",
    )
    parser.add_argument(
        "--device",
        type=str,
        choices=["auto", "cuda", "mps", "cpu"],
        default="auto",
        help="Device to use for transformers backend (default: auto)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="Maximum completion tokens per query (default: 256)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Generation temperature (default: 0.2)",
    )
    args = parser.parse_args()

    if args.backend == "vllm":
        run_via_vllm_api(
            api_base=args.api_base,
            model_id=args.model_id,
            prompts=GENERIC_QUERIES,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
    else:
        run_via_transformers(
            model_id=args.model_id,
            prompts=GENERIC_QUERIES,
            max_tokens=args.max_tokens,
            device_arg=args.device,
            temperature=args.temperature,
        )


if __name__ == "__main__":
    main()
