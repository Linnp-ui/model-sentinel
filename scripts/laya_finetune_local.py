"""Laya 本地微调（单卡 4090，审计语料+合成增广，全离线）

由官方 notebook（2xT4 DDP）适配：去掉 DDP，数据源换本地 JSONL，
训练循环/损失/调度/温度拟合与官方一致（RLCD：GRPO 基线 + proper scoring + 软 CE）。

用法:
  CUDA_VISIBLE_DEVICES=6 ~/venvs/laya/bin/python scripts/laya_finetune_local.py \
    ~/models/laya/multilingual ~/models/laya-dataset ~/models/laya-finetuned-confidential

⚠️ QUESTIONS 必须与 scripts/laya_l2_server.py 的 QUESTIONS 逐字一致（训练/推理同题规格）。
"""
import json
import math
import os
import random
import sys
import time

import torch
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from laya.common import build_model, build_sequence, proper_reward, render_options, QTYPES

# 与 laya_l2_server.py QUESTIONS 保持一致（noul 英文题，2026-09-22 探针定规格）
QUESTIONS = {
    "is_confidential": {
        "type": "noul",
        "instructions": (
            "Is this content enterprise-confidential (engineering drawings, financial data like "
            "salaries/costs/revenue, personal sensitive data like ID/phone/bank card, or business "
            "secrets like contracts/bids/internal docs)? A table with employee names and salary "
            "amounts is confidential."
        ),
        "criteria": {
            "true": "contains confidential content",
            "false": "public or everyday content",
        },
    },
}


def render_state(text: str, filename: str) -> str:
    # 与 laya_l2_server._render_state 同布局（生产 L2 输入形态）
    if filename:
        return f"文件名: {filename}\n\n内容预览:\n{text}"
    return f"内容预览:\n{text}"


def fit_one_temp(sel):
    if len(sel) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in sel)
    Z = torch.full((len(sel), kmax), -1e4)
    T = torch.zeros((len(sel), kmax))
    for i, (z, t) in enumerate(sel):
        Z[i, :len(z)] = torch.tensor(z)
        T[i, :len(t)] = torch.tensor(t, dtype=torch.float32)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss
    opt.step(closure)
    return float(torch.clamp(log_t.exp(), 0.1, 10.0).item())


def collate_train_batch(items, pad_id):
    n, L = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, :len(it["ids"])] = torch.tensor(it["ids"])
        att[i, :len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, :len(it["target"])] = torch.tensor(it["target"], dtype=torch.float32)
    return {
        "input_ids": ids,
        "attention_mask": att,
        "marker_pos": mpos,
        "marker_mask": mmask,
        "target": target,
        "qtype": torch.tensor([it["qtype"] for it in items]),
        "label": torch.tensor([it["label"] for it in items]),
    }


def main():
    model_dir, dataset_dir, output_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    device = torch.device("cuda", 0)
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    with open(os.path.join(model_dir, "rl_agent_config.json")) as f:
        cfg = json.load(f)
    cfg["gradient_checkpointing"] = True
    cfg["max_tokens_per_batch"] = 4096
    cfg["max_len"] = 1024
    cfg["head_max_len"] = 256

    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder"))
    model.load_state_dict(load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True
    model.to(device)
    model.train()

    # ---- 数据：JSONL → 训练序列（noul: target=[P(false), P(true)]）
    rows = []
    with open(os.path.join(dataset_dir, "train.jsonl"), encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    q = QUESTIONS["is_confidential"]
    items = []
    for row in rows:
        state = render_state(row["text"], row.get("filename", ""))
        p_true = 1.0 if row["label"] == 1 else 0.0
        target = [1.0 - p_true, p_true]
        seq, markers = build_sequence(tok, state,
                                      {"t": q["type"], "ins": q["instructions"], "crit": q["criteria"]},
                                      cfg["max_len"], cfg["head_max_len"])
        if len(markers) != len(render_options({"t": q["type"], "crit": q["criteria"]})):
            continue
        items.append({"ids": seq, "markers": markers, "qtype": QTYPES[q["type"]],
                      "target": target, "label": 1 if p_true >= 0.5 else 0})
    print(f"Preprocessed {len(items)} training sequences "
          f"(pos={sum(it['label'] for it in items)}, neg={sum(1 - it['label'] for it in items)})")

    EPOCHS = int(os.getenv("LAYA_FT_EPOCHS", "4"))
    MICRO_BATCH = 8
    GRAD_ACCUM = 4
    GROUP_SIZE = 4
    LR_ENCODER = 2.5e-5
    LR_HEAD = 1.0e-4
    SIGMA_START, SIGMA_END = 0.4, 0.1

    optimizer = torch.optim.AdamW([
        {"params": [p for n, p in model.named_parameters() if "encoder." in n], "lr": LR_ENCODER},
        {"params": [p for n, p in model.named_parameters() if "encoder." not in n], "lr": LR_HEAD},
    ], weight_decay=0.01)
    total_updates = max(1, (len(items) // (MICRO_BATCH * GRAD_ACCUM)) * EPOCHS)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_updates, eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda", enabled=True)

    t0 = time.time()
    for epoch in range(EPOCHS):
        random.seed(42 + epoch)
        random.shuffle(items)
        epoch_loss, n_batches = 0.0, 0
        optimizer.zero_grad(set_to_none=True)
        accum_step = 0
        sigma = SIGMA_START + (SIGMA_END - SIGMA_START) * epoch / max(1, EPOCHS - 1)

        for b_idx in range(0, len(items), MICRO_BATCH):
            chunk = items[b_idx:b_idx + MICRO_BATCH]
            if not chunk:
                continue
            batch = collate_train_batch(chunk, tok.pad_token_id)
            with torch.autocast("cuda", dtype=torch.float16):
                logits, act = model(
                    batch["input_ids"].to(device), batch["attention_mask"].to(device),
                    batch["marker_pos"].to(device), batch["marker_mask"].to(device),
                    batch["qtype"].to(device))
            logits = logits.float()
            mask = batch["marker_mask"].to(device)
            k = mask.sum(-1, keepdim=True).float()
            target = batch["target"].to(device)

            eps = torch.randn((GROUP_SIZE,) + logits.shape, device=device) * sigma * mask
            eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
            z = logits.detach().unsqueeze(0) + eps
            q_s = torch.softmax(z.masked_fill(~mask, -1e4), -1)
            with torch.no_grad():
                r = proper_reward(q_s, target.unsqueeze(0), batch["qtype"].to(device), mask,
                                  w_sph=0.75, w_rps=1.0)
                adv = r - r.mean(0, keepdim=True)
                adv = adv / (adv.std() + 1e-6)
            logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
            loss_rl = -(adv * logp).mean()
            loss_ce = -(target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1).mean()
            loss = (loss_rl + 1.0 * loss_ce) / GRAD_ACCUM + 0.0 * act.sum()

            scaler.scale(loss).backward()
            accum_step += 1
            if accum_step % GRAD_ACCUM == 0 or (b_idx + MICRO_BATCH) >= len(items):
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            epoch_loss += loss.item() * GRAD_ACCUM
            n_batches += 1
            if n_batches % 50 == 0:
                print(f"  Epoch {epoch+1}/{EPOCHS} | Step {n_batches} | Loss: {loss.item()*GRAD_ACCUM:.4f} "
                      f"| Reward: {r.mean().item():.3f} | LR: {scheduler.get_last_lr()[0]:.2e}")

        print(f"=== Epoch {epoch+1}/{EPOCHS} done in {time.time()-t0:.0f}s | Avg Loss: {epoch_loss/max(1,n_batches):.4f} ===")
        ckpt_dir = os.path.join(output_dir, "checkpoint_latest")
        os.makedirs(ckpt_dir, exist_ok=True)
        sd = {key: v.half().contiguous().cpu() for key, v in model.state_dict().items()}
        save_file(sd, os.path.join(ckpt_dir, "model.safetensors"))
        model.encoder.config.save_pretrained(os.path.join(ckpt_dir, "encoder"))
        tok.save_pretrained(os.path.join(ckpt_dir, "tokenizer"))
        with open(os.path.join(ckpt_dir, "checkpoint_meta.json"), "w") as f:
            json.dump({"epoch": epoch + 1, "total_epochs": EPOCHS,
                       "avg_loss": epoch_loss / max(1, n_batches)}, f, indent=2)
        print(f"  Rolling checkpoint (epoch {epoch+1}/{EPOCHS}) → {ckpt_dir}")

    # ---- 温度拟合 + 最终保存（与官方一致；fit_one_temp 即 README 要求的"自己数据上 refit"）
    print("\nFitting calibration temperature...")
    del optimizer, scaler, scheduler
    torch.cuda.empty_cache()
    model.eval()
    calib_items = items[::15][:400]
    calib_preds = []
    with torch.no_grad():
        for c_idx in range(0, len(calib_items), 16):
            c_chunk = calib_items[c_idx:c_idx + 16]
            cb = collate_train_batch(c_chunk, tok.pad_token_id)
            with torch.autocast("cuda", dtype=torch.float16):
                l_sub, _ = model(cb["input_ids"].to(device), cb["attention_mask"].to(device),
                                 cb["marker_pos"].to(device), cb["marker_mask"].to(device),
                                 cb["qtype"].to(device))
            l_np = l_sub.float().cpu().numpy()
            for r_i, it in enumerate(c_chunk):
                calib_preds.append((it["qtype"], l_np[r_i, :len(it["markers"])], it["target"]))
    fitted_temps = [1.2, 1.2, 1.2]
    for qt in range(3):
        sel = [(z, t) for q_t, z, t in calib_preds if q_t == qt]
        if sel:
            fitted_temps[qt] = fit_one_temp(sel)
    print("Fitted temperatures (choice, score, noul):", [round(t, 3) for t in fitted_temps])

    os.makedirs(output_dir, exist_ok=True)
    sd = {key: v.half().contiguous().cpu() for key, v in model.state_dict().items()}
    save_file(sd, os.path.join(output_dir, "model.safetensors"))
    model.encoder.config.save_pretrained(os.path.join(output_dir, "encoder"))
    tok.save_pretrained(os.path.join(output_dir, "tokenizer"))
    cfg["fine_tuned"] = True
    cfg["model_name"] = "laya-confidential-zh"
    cfg["temperature"] = fitted_temps
    with open(os.path.join(output_dir, "rl_agent_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"Saved fine-tuned model to {output_dir}")


if __name__ == "__main__":
    main()
