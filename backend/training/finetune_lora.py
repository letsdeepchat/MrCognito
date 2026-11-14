"""QLoRA fine-tune of an instruction model on the Socratic-coach SFT set.

Requires ``pip install -r requirements-ml.txt`` and a CUDA GPU. The resulting
adapter is served with vLLM (``--enable-lora``) behind an OpenAI-compatible
endpoint, then selected with ``COGNITO_LLM_PROVIDER=openai_compat``.

    python -m training.finetune_lora --base Qwen/Qwen2.5-7B-Instruct --data data/sft.jsonl --out out/coach-lora
"""
from __future__ import annotations

import argparse


def main() -> None:  # pragma: no cover - GPU job
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="HF model id of an instruction-tuned base")
    ap.add_argument("--data", default="data/sft.jsonl")
    ap.add_argument("--out", default="out/coach-lora")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=2048)
    a = ap.parse_args()

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    tok = AutoTokenizer.from_pretrained(a.base)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        a.base,
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                               bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True),
        device_map="auto",
    )
    model = prepare_model_for_kbit_training(model)
    ds = load_dataset("json", data_files=a.data, split="train").train_test_split(test_size=0.05, seed=0)
    trainer = SFTTrainer(
        model=model,
        processing_class=tok,
        train_dataset=ds["train"],
        eval_dataset=ds["test"],
        peft_config=LoraConfig(r=a.rank, lora_alpha=2 * a.rank, lora_dropout=0.05, task_type="CAUSAL_LM",
                               target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]),
        args=SFTConfig(output_dir=a.out, num_train_epochs=a.epochs, learning_rate=a.lr, per_device_train_batch_size=4,
                       gradient_accumulation_steps=4, lr_scheduler_type="cosine", warmup_ratio=0.03, bf16=True,
                       logging_steps=10, eval_strategy="steps", eval_steps=100, save_steps=200,
                       max_length=a.max_len, assistant_only_loss=True),
    )
    trainer.train()
    trainer.save_model(a.out)
    print(f"adapter saved to {a.out}")


if __name__ == "__main__":
    main()
