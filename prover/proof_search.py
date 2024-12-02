"""Proof search using best-first search.
"""

import sys
import ray
import time
import uuid
import torch
import asyncio
from lean_dojo import (
    Pos,
    Dojo,
    Theorem,
    LeanGitRepo,
    TacticState,
    LeanError,
    TimeoutError,
    ProofFinished,
    ProofGivenUp,
    DojoInitError,
    DojoCrashError,
    DojoTacticTimeoutError,
)
from loguru import logger
from dataclasses import dataclass
from typing import List, Optional, Tuple
from ray.util.actor_pool import ActorPool
from vllm import AsyncLLMEngine, AsyncEngineArgs, SamplingParams, RequestOutput

from common import zip_strict
from prover.search_tree import *
from prover.tactic_generator import (
    TacticGenerator,
    HuggingFaceGenerator,
    RetrievalAugmentedGenerator,
    FixedTacticGenerator,
    VllmGenerator,
)

# 存储尝试证明的结果
@dataclass(frozen=True)
class SearchResult:
    """The result of attempting to prove a theorem."""

    theorem: Theorem    # 待证明的定理
    status: Status  # 证明状态(成功/失败/开放)
    proof: Optional[List[str]]  # 证明步骤列表

    # Some statistics during proof search.
    actor_time: float   # 模型推理时间
    environment_time: float # 环境执行时间
    total_time: float   # 总时间
    num_total_nodes: int    # 搜索树中节点数量
    num_searched_nodes: int     # 搜索过的节点数量(扩展次数)(包含重复节点)


# 使用最佳优先搜索算法 (best-first search) 的定理证明器
class BestFirstSearchProver:
    """A prover that uses best-first search to find proofs using a tactic generator."""

    def __init__(
        self,
        tac_gen,  # A given tactic generator.
        timeout: int,
        max_expansions: Optional[int],
        num_sampled_tactics: int,
        debug: bool,
    ) -> None:
        self.tac_gen = tac_gen  # 策略生成器
        self.tac_gen.initialize()
        self.timeout = timeout  # 超时限制
        self.max_expansions = max_expansions  # 最大扩展次数
        self.num_sampled_tactics = num_sampled_tactics
        self.debug = debug

        self.num_expansions = 0
        self.actor_time = 0.0
        self.environment_time = 0.0
        self.total_time = None

    # 证明定理, 返回结果
    def search(
        self, repo: LeanGitRepo, thm: Theorem, pos: Pos
    ) -> Optional[SearchResult]:
        logger.info(f"Proving {thm}")

        self.repo = repo
        self.theorem = thm
        self.posision = pos
        self.actor_time = 0.0
        self.environment_time = 0.0
        self.num_expansions = 0

        if isinstance(self.tac_gen, FixedTacticGenerator):
            imps = [self.tac_gen.module]
        else:
            imps = []

        try:
            # 开始定理证明
            with Dojo(thm, self.timeout, additional_imports=imps) as (
                dojo,
                init_state,
            ):
                self.dojo = dojo
                self.root = InternalNode(
                    state=init_state,
                    cumulative_logprob=0.0,
                )
                self.nodes = {init_state: self.root}

                try:
                    # asyncio.run 是一个用于运行异步协程的函数，它会创建一个新的事件循环并在该循环中运行传入的协程，直到协程完成。
                    asyncio.run(self._best_first_search())
                except DojoCrashError as ex:
                    logger.warning(f"Dojo crashed with {ex} when proving {thm}")
                    pass

            if self.root.status == Status.PROVED:
                proof = [e.tactic for e in self.root.extract_proof()]
            else:
                proof = None

            result = SearchResult(
                theorem=thm,
                status=self.root.status,
                proof=proof,
                actor_time=self.actor_time,
                environment_time=self.environment_time,
                total_time=self.total_time,
                num_total_nodes=len(self.nodes),
                num_searched_nodes=self.num_expansions,
            )
            logger.info(result)
            return result

        except DojoInitError as ex:
            logger.warning(ex)
            return None

    # 1. 初始化优先队列
    # 2. 循环直到找到证明/失败/超时
    # 3. 每一步选择最优节点进行扩展
    async def _best_first_search(self) -> None:     # async 是 Python 中用于定义异步函数的关键字
        time_start = time.time()

        priority_queue = asyncio.PriorityQueue()
        # priority: cumulative_logprob
        priority_queue.put_nowait((-self.root.priority, self.root))

        while True:
            if priority_queue.empty():
                logger.info("Ran out of nodes to search.")
                break

            try:
                await self._step(priority_queue)
            except DojoTacticTimeoutError:
                assert time.time() - time_start >= self.timeout

            self.total_time = time.time() - time_start
            # 到了限定时间 或 扩张次数大于最大扩张次数
            if self.total_time > self.timeout or (
                self.max_expansions is not None
                and self.num_expansions > self.max_expansions
            ):
                if self.root.status == Status.PROVED:
                    logger.info("Found a proof!")
                self.root.status = Status.OPEN
                logger.info("Hit the resource limit (timeout or max_expansions).")
                break

            if self.root.status == Status.FAILED:
                logger.info("Failed early!")
                break

            if self.root.status == Status.PROVED:
                logger.info("Found a proof!")
                break

    # 选取优先级最高的 node, 使用 generator 生成策略，尝试每个策略
    # 将结果存在 search_node 的 out_edges 中
    async def _step(self, priority_queue):
        """
        Perform a single step of search.

        Selects the node with the highest priority, queries the model for suggested
        tactics, and tries each tactic in the environment, creating and enqueuing
        a new node for each valid result.
        """
        # Search the node with highest priority.
        try:
            _, search_node = priority_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        logger.debug(f"Expanding node: {search_node}")

        if isinstance(search_node.state, TacticState):
            ts = search_node.state.pp
        else:
            ts = search_node.state.unsolved_tactic_state
        suggestions = await self._generate_tactics(ts)

        # Try all tactics in order of descending logprob, and collect the results. Any
        # new nodes are added to `self.nodes`, and edges are added to the result node.
        results = []
        for tactic, logprob in suggestions:
            edge, finished = self._run_tactic(
                search_node, tactic, logprob, priority_queue
            )
            results.append(edge)
            if finished:
                break

        # Store the fixed out edges of this node, marking it as explored.
        # This will trigger recursively recomputing tree statistics.
        search_node.out_edges = results
        self.num_expansions += 1
        priority_queue.task_done()

        # If we're running in debug mode, run a full test suite each step
        if self.debug:
            assert self.num_expansions == sum(
                node.is_explored
                for node in self.nodes.values()
                if isinstance(node, InternalNode)
            )
            self.check_invariants()

    # 使用策略生成器生成下一步可能的证明策略
    @torch.no_grad()
    async def _generate_tactics(self, ts: str) -> List[Tuple[str, float]]:
        t0 = time.time()

        path = str(self.theorem.file_path)

        # 如果 theorem 不在当前 repo 中，需要指定完整路径
        if self.theorem.repo != self.repo:
            path = self.theorem.repo.get_packages_dir() / self.theorem.repo.name / path

        suggestions = await self.tac_gen.generate(
            state=ts,
            file_path=path,
            theorem_full_name=self.theorem.full_name,
            theorem_pos=self.posision,
            num_samples=self.num_sampled_tactics,
        )

        self.actor_time += time.time() - t0

        logger.debug(f"Tactic suggestions: {suggestions}")
        return suggestions

    # 在证明环境中执行策略
    # 将新得到的状态计算优先级, 加入优先队列中
    # 返回原node到新node的边, 以及是否证明成功
    def _run_tactic(
        self, node: InternalNode, tactic: str, logprob: float, priority_queue
    ) -> Tuple[Edge, bool]:
        t0 = time.time()
        response = self.dojo.run_tac(node.state, tactic)

        elapsed = time.time() - t0
        self.environment_time += elapsed

        try:
            # If we've seen this response before, use the existing node
            result_node = self.nodes[response]
        except KeyError:
            # Build a new node
            if isinstance(response, ProofFinished):
                result_node = ProofFinishedNode(response)
            elif type(response) in (
                LeanError,
                TimeoutError,
                ProofGivenUp,
            ):
                result_node = ErrorNode(response)
            else:
                assert isinstance(response, TacticState)
                result_node = InternalNode(
                    state=response,
                    cumulative_logprob=logprob + node.cumulative_logprob,
                )

            if result_node.status == Status.OPEN:  # Don't search proved/failed nodes
                priority_queue.put_nowait((-result_node.priority, result_node))

        # Record the new node and add it to the search queue.
        self.nodes[response] = result_node

        # Build an edge connecting these nodes.
        # Will be added to the source node externally.
        edge = Edge(tactic=tactic, src=node, dst=result_node)

        if isinstance(result_node, InternalNode):
            result_node.in_edges.append(edge)

        return edge, isinstance(response, ProofFinished)

    #########
    # DEBUG #
    #########

    def check_invariants(self):
        """Perform some sanity checks."""

        for response, node in self.nodes.items():
            if isinstance(response, ProofFinished):
                assert isinstance(node, ProofFinishedNode)
                assert self.root.status == Status.PROVED
            elif type(response) in (
                LeanError,
                TimeoutError,
                ProofGivenUp,
            ):
                assert isinstance(node, ErrorNode)
            else:
                assert isinstance(node, InternalNode)
                node.check_invariants()


# Ray actor 用于并行运行多个证明器实例
@ray.remote
class ProverActor:
    """Ray actor for running an instance of `BestFirstSearchProver`."""

    def __init__(
        self,
        tac_gen: TacticGenerator,
        timeout: int,
        max_expansions: Optional[int],
        num_sampled_tactics: int,
        debug: bool,
    ) -> None:
        self.prover = BestFirstSearchProver(
            tac_gen,
            timeout,
            max_expansions,
            num_sampled_tactics,
            debug,
        )

    def search(
        self, repo: LeanGitRepo, thm: Theorem, pos: Pos
    ) -> Optional[SearchResult]:
        return self.prover.search(repo, thm, pos)


@ray.remote
class VllmActor:
    """Ray actor for running an instance of `vllm.AsyncLLMEngine`, which is shared by all `ProverActor` instances."""

    def __init__(self, model_path: str) -> None:
        self.num_gpus = len(ray.get_gpu_ids())
        self.model_path = model_path

    def initialize(self) -> None:
        logger.info("Initializing vLLM")
        # TODO: Try other options in https://docs.vllm.ai/en/stable/models/engine_args.html#engine-args.
        engine_args = AsyncEngineArgs(
            model=self.model_path,
            tensor_parallel_size=self.num_gpus,
            max_num_batched_tokens=8192,
            # max_num_batched_tokens=2048,
            # enable_chunked_prefill=True,
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)

    async def generate(self, prompt: str, num_samples: int) -> RequestOutput:
        sampling_params = SamplingParams(
            n=num_samples,
            temperature=0,
            length_penalty=0,
            use_beam_search=True,
            early_stopping=False,
            logprobs=0,
        )

        async for oup in self.engine.generate(
            prompt, sampling_params, request_id=str(uuid.uuid4().hex)
        ):
            final_output = oup
        return final_output


# 使用 Ray 来并行 proof search
class DistributedProver:
    """A distributed prover that uses Ray to parallelize the proof search.

    It is a wrapper around `ProverActor` that handles the different
    devices and different number of concurrent provers.
    """

    def __init__(
        self,
        use_vllm: bool,
        gen_ckpt_path: Optional[str],
        ret_ckpt_path: Optional[str],
        indexed_corpus_path: Optional[str],
        max_inp_seq_len: int,
        max_oup_seq_len: int,
        length_penalty: float,
        tactic: Optional[str],
        module: Optional[str],
        num_workers: int,
        num_gpus: int,
        timeout: int,
        max_expansions: Optional[int],
        num_sampled_tactics: int,
        debug: Optional[bool] = False,
    ) -> None:
        if gen_ckpt_path is None:
            assert tactic and not indexed_corpus_path
        else:
            assert not tactic and not module

        if gen_ckpt_path is None:
            tac_gen = FixedTacticGenerator(tactic, module)
        elif use_vllm:
            assert indexed_corpus_path is None
            vllm_actor = VllmActor.options(num_gpus=num_gpus).remote(gen_ckpt_path)
            ray.get(vllm_actor.initialize.remote())
            # 使用 VllmGenerator 加载 tactic generator
            tac_gen = VllmGenerator(vllm_actor)
        elif indexed_corpus_path is not None:
            device = torch.device("cuda") if num_gpus > 0 else torch.device("cpu")
            tac_gen = RetrievalAugmentedGenerator(
                gen_ckpt_path,
                ret_ckpt_path,
                indexed_corpus_path,
                device,
                max_inp_seq_len,
                max_oup_seq_len,
                length_penalty,
                max_num_retrieved=100,
            )
        else:
            device = torch.device("cuda") if num_gpus > 0 else torch.device("cpu")
            tac_gen = HuggingFaceGenerator(
                gen_ckpt_path, device, max_inp_seq_len, max_oup_seq_len, length_penalty
            )

        # 是否进行分布式计算
        self.distributed = num_workers > 1
        if not self.distributed:
            assert num_gpus <= 1
            # 如果不进行分布式计算，直接使用单个证明器
            self.prover = BestFirstSearchProver(
                tac_gen, timeout, max_expansions, num_sampled_tactics, debug
            )
            return

        # 使用分布式计算
        if num_gpus >= 1:
            logger.info(f"Launching {num_workers} workers with {num_gpus} GPUs.")
            if use_vllm:
                # GPUs are managed by `VllmActor`.
                num_gpus_per_worker = 0
            else:
                num_gpus_per_worker = num_gpus / num_workers
            # 加载 num_workers 个 prover
            provers = [
                ProverActor.options(num_gpus=num_gpus_per_worker).remote(
                    tac_gen,
                    timeout=timeout,
                    max_expansions=max_expansions,
                    num_sampled_tactics=num_sampled_tactics,
                    debug=debug,
                )
                for _ in range(num_workers)
            ]
        else:
            logger.info(f"Launching {num_workers} CPU workers.")
            provers = [
                ProverActor.remote(
                    tac_gen,
                    timeout=timeout,
                    max_expansions=max_expansions,
                    num_sampled_tactics=num_sampled_tactics,
                    debug=debug,
                )
                for _ in range(num_workers)
            ]

        # 初始化一个 ActorPool 实例，并将一组 provers 分配给这个池子，以便进行并行处理。
        self.prover_pool = ActorPool(provers)

    def search_unordered(
        self, repo: LeanGitRepo, theorems: List[Theorem], positions: List[Pos]
    ) -> List[Optional[SearchResult]]:
        """Parallel proof search for `theorems`. The order of the results is not guaranteed to match the order of the input."""
        # self.distributed: 是否进行分布式计算
        if not self.distributed:
            return [
                self.prover.search(repo, thm, pos)
                for thm, pos in zip_strict(theorems, positions)
            ]

        try:
            results = list(
                self.prover_pool.map_unordered(
                    lambda p, x: p.search.remote(repo, x[0], x[1]),
                    zip_strict(theorems, positions),
                )
            )
        except ray.exceptions.RayActorError as ex:
            logger.error(ex)
            sys.exit(1)

        return results
