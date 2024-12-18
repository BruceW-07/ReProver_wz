"""Lightning module for the tactic generator."""

import os
import torch
import shutil
import pickle
from loguru import logger
import pytorch_lightning as pl
from torchmetrics import Metric
from typing import List, Dict, Any, Optional
from transformers import T5ForConditionalGeneration, AutoTokenizer

from common import (
    remove_marks,
    IndexedCorpus,
    get_optimizers,
    load_checkpoint,
)
from retrieval.model import PremiseRetriever


torch.set_float32_matmul_precision("medium")

# 计算前 k 正确率: 对每个 state, 使用束搜索 (beam search) 生成 k 个 tactics. 若这 k 个 tactics 中包含 state 对应的 tactic, 则判定为正确.
class TopkAccuracy(Metric):
    is_differentiable: Optional[bool] = False
    higher_is_better: Optional[bool] = True
    full_state_update: bool = True

    def __init__(self, k: int) -> None:
        super().__init__()
        self.k = k
        self.add_state("correct", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("total", default=torch.tensor(0), dist_reduce_fx="sum")

    # 新加入一个 batch 更新 topk 准确率
    def update(self, batch_preds: List[List[str]], batch_gt: List[str]):
        assert len(batch_preds) == len(batch_gt)
        for preds, gt in zip(batch_preds, batch_gt):
            # This still doesn't account for short names vs. full names.
            gt = remove_marks(gt)
            preds = [remove_marks(p) for p in preds]
            self.correct += gt in preds[: self.k]
        self.total += len(batch_gt)

    # 计算总的 topk 准确率
    def compute(self) -> float:
        return self.correct.float() / self.total

class RetrievalAugmentedGenerator(pl.LightningModule):
    def __init__(
        self,
        model_name: str,
        lr: float,
        warmup_steps: int,
        num_beams: int,
        eval_num_retrieved: int,
        eval_num_workers: int,
        eval_num_gpus: int,
        eval_num_theorems: int,
        max_inp_seq_len: int,
        max_oup_seq_len: int,
        length_penalty: float = 0.0,
        ret_ckpt_path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()
        self.lr = lr
        self.warmup_steps = warmup_steps
        self.num_beams = num_beams
        self.length_penalty = length_penalty
        self.eval_num_retrieved = eval_num_retrieved
        self.eval_num_workers = eval_num_workers
        self.eval_num_gpus = eval_num_gpus
        self.eval_num_theorems = eval_num_theorems
        self.max_inp_seq_len = max_inp_seq_len
        self.max_oup_seq_len = max_oup_seq_len

        # 获取 retriever
        if ret_ckpt_path is None:
            self.retriever = None
        else:
            logger.info(f"Loading the retriever from {ret_ckpt_path}")
            self.retriever = PremiseRetriever.load(
                ret_ckpt_path, self.device, freeze=True
            )

        # 获取已 pretrain 的模型 tokenizer 和 generator
        # model_name 在 confs/cli_lean4_{...}.yaml 中指定
        self.tokenizer = AutoTokenizer.from_pretrained(model_name) 
        self.generator = T5ForConditionalGeneration.from_pretrained(model_name)

        # 初始化 topk accuracies, 用于衡量模型生成的前 k 个预测结果中是否包含正确答案
        self.topk_accuracies = dict()
        for k in range(1, num_beams + 1):
            acc = TopkAccuracy(k)
            self.topk_accuracies[k] = acc
            self.add_module(f"top{k}_acc_val", acc)

    # 载入模型的 checkpoint
    @classmethod
    def load(
        cls, ckpt_path: str, device, freeze: bool
    ) -> "RetrievalAugmentedGenerator":
        return load_checkpoint(cls, ckpt_path, device, freeze)

    # 使用 generator 根据 state 生成 tactic 并返回 loss
    def forward(
        self,
        state_ids: torch.Tensor,
        state_mask: torch.Tensor,
        tactic_ids: torch.Tensor,
    ) -> torch.Tensor:
        return self.generator(
            input_ids=state_ids,
            attention_mask=state_mask,
            labels=tactic_ids,
        ).loss

    ############
    # Training #
    ############

    # 训练一个 batch, 返回 loss
    def training_step(self, batch, batch_idx: int):
        # 模型前向传播：通过调用 self(...) 处理输入数据，包括状态 ID (state_ids)、状态掩码 (state_mask) 和策略 ID (tactic_ids)，计算得到损失值。
        # (pytorch 特性, 使用 self(...) 代替 self.forward(...))
        loss = self(
            batch["state_ids"],
            batch["state_mask"],
            batch["tactic_ids"],
        )
        self.log(
            "loss_train",
            loss,
            on_step=True,
            on_epoch=True,
            sync_dist=True,
            batch_size=len(batch),
        )
        self._log_io_texts("train", batch["state_ids"], batch["tactic_ids"])
        return loss

    # 根据参数返回优化器
    def configure_optimizers(self) -> Dict[str, Any]:
        return get_optimizers(
            self.parameters(), self.trainer, self.lr, self.warmup_steps
        )

    # 以文本形式记录输入输出
    def _log_io_texts(
        self,
        split: str,
        state_ids: torch.LongTensor,
        tactic_ids: torch.LongTensor,
    ) -> None:
        inp = self.tokenizer.decode(state_ids[0], skip_special_tokens=True)
        # 检查 tactic_ids[0] 中的每个元素是否等于 -100。
        # 如果条件为真，则选择 self.tokenizer.pad_token_id（填充标识符）;
        # 如果条件为假，则选择 tactic_ids[0] 中的原始值
        oup_ids = torch.where(
            tactic_ids[0] == -100, self.tokenizer.pad_token_id, tactic_ids[0]
        )
        oup = self.tokenizer.decode(oup_ids, skip_special_tokens=True)
        # 以文本形式记录 state 和 tactic
        self.logger.log_text(
            f"{split}_samples",
            ["state", "tactic"],
            [[inp, oup]],
            step=self.global_step,
        )

    # 训练开始时记录超参数, 加载文本集
    def on_fit_start(self) -> None:
        if self.logger is not None:
            self.logger.log_hyperparams(self.hparams)
            assert self.trainer is not None
            logger.info(f"Logging to {self.trainer.log_dir}")

        if self.retriever is not None:
            self.retriever.load_corpus(self.trainer.datamodule.corpus)

    ##############
    # Validation #
    ##############

    # 验证一个 batch, 记录 topk 准确率
    def validation_step(self, batch: Dict[str, Any], _) -> None:
        state_ids = batch["state_ids"]
        state_mask = batch["state_mask"]
        tactic_ids = batch["tactic_ids"]

        # 调用 forward 函数计算 loss 
        loss = self(state_ids, state_mask, tactic_ids)
        self.log(f"loss_val", loss, on_step=False, on_epoch=True, sync_dist=True)
        self._log_io_texts("val", state_ids, tactic_ids)

        # Generate topk tactic candidates via Beam Search.
        output = self.generator.generate(
            input_ids=state_ids,
            attention_mask=state_mask,
            max_length=self.max_oup_seq_len,
            num_beams=self.num_beams,
            do_sample=False,
            num_return_sequences=self.num_beams,
            early_stopping=False,
        )
        # 将生成的策略转换为文本
        output_text = self.tokenizer.batch_decode(output, skip_special_tokens=True)
        batch_size = state_ids.size(0)
        assert len(output_text) == batch_size * self.num_beams
        # 将生成的策略按对应的 state 分组 (一个 state 对应 num_beams 个策略)
        tactics_pred = [
            output_text[i * self.num_beams : (i + 1) * self.num_beams]
            for i in range(batch_size)
        ]

        msg = "\n".join(tactics_pred[0])
        self.logger.log_text("preds_val", ["tactics"], [[msg]], step=self.global_step)

        # Log the topk accuracies.
        # 记录 topk 准确率
        for k in range(1, self.num_beams + 1):
            topk_acc = self.topk_accuracies[k]
            topk_acc(tactics_pred, batch["tactic"])
            self.log(
                f"top{k}_acc_val",
                topk_acc,
                on_step=False,
                on_epoch=True,
                sync_dist=True,
            )

    # 在验证结束后保存模型
    def on_validation_epoch_end(self) -> None:
        if self.eval_num_theorems == 0 or self.logger is None:
            return

        from prover.evaluate import evaluate  # Avoid circular import.

        # 保存 generator 和 tokenizer
        gen_ckpt_path = f"{self.trainer.log_dir}/last-generator"
        ret_ckpt_path = f"{self.trainer.log_dir}/last-retriever"
        indexed_corpus_path = f"{self.trainer.log_dir}/last-indexed-corpus.pickle"
        self.generator.save_pretrained(gen_ckpt_path)
        self.tokenizer.save_pretrained(gen_ckpt_path)

        # 保存 retriever 和 indexed corpus (文本集)
        if self.retriever is not None:
            self.retriever.encoder.save_pretrained(ret_ckpt_path)
            self.retriever.tokenizer.save_pretrained(ret_ckpt_path)
            self.retriever.reindex_corpus(self.trainer.datamodule.eval_batch_size)
            pickle.dump(
                IndexedCorpus(
                    self.retriever.corpus, self.retriever.corpus_embeddings.cpu()
                ),
                open(indexed_corpus_path, "wb"),
            )
            torch.cuda.empty_cache()
            # 评估整个 prover 的性能
            acc = evaluate(
                data_path=self.trainer.datamodule.data_path,
                num_workers=self.eval_num_workers,
                num_gpus=self.eval_num_gpus,
                num_theorems=self.eval_num_theorems,
                gen_ckpt_path=gen_ckpt_path,
                ret_ckpt_path=ret_ckpt_path,
                indexed_corpus_path=indexed_corpus_path,
            )
        else:
            torch.cuda.empty_cache()
            acc = evaluate(
                data_path=self.trainer.datamodule.data_path,
                num_workers=self.eval_num_workers,
                num_gpus=self.eval_num_gpus,
                num_theorems=self.eval_num_theorems,
                gen_ckpt_path=gen_ckpt_path,
            )

        self.log("Pass@1_val", acc, on_step=False, on_epoch=True, sync_dist=True)
        logger.info(f"Pass@1: {acc}")

        for path in [gen_ckpt_path, ret_ckpt_path, indexed_corpus_path]:
            if os.path.exists(path):
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
