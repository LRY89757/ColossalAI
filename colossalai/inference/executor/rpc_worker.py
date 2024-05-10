import torch
import torch.distributed as dist
from torch import nn

import rpyc

from colossalai.shardformer.policies.base_policy import Policy

from transformers.models.llama.modeling_llama import LlamaForCausalLM

from typing import List, Union, Tuple
from colossalai.accelerator import get_accelerator
from colossalai.inference.modeling.policy import model_policy_map
from colossalai.inference.flash_decoding_utils import FDIntermTensors
from colossalai.inference.utils import get_model_size, has_index_file, find_available_ports
from colossalai.cluster import ProcessGroupMesh
from colossalai.interface import ModelWrapper
from colossalai.inference.rpc_config import InferenceConfig, InputMetaData
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.shardformer import ShardConfig, ShardFormer
from colossalai.shardformer.policies.base_policy import Policy
from colossalai.inference.modeling.policy import NoPaddingLlamaModelInferPolicy, NoPaddingBaichuanModelInferPolicy
from colossalai.logging import get_dist_logger
import colossalai

from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
)


PP_AXIS, TP_AXIS = 0, 1

_supported_models = {
    "LlamaForCausalLM": LlamaForCausalLM,
    "BaichuanForCausalLM": AutoModelForCausalLM,
}

_supported_model_policies = {
    "NoPaddingLlamaModelInferPolicy": NoPaddingLlamaModelInferPolicy,
    "NoPaddingBaichuanModelInferPolicy": NoPaddingBaichuanModelInferPolicy,
}

logger = get_dist_logger(__name__)

class rpcWorkerService(rpyc.Service):

    """
    Execute the computation tasks and manage its own kv cache

    Func with prefix `exposed_` will be invoked by client.
    """

    def exposed_init_dist_env(self, rank, world_size, master_address, master_port):
        logger.info(f"init process group for rank {rank}")
        colossalai.launch(config={}, rank=rank, world_size=world_size, port=master_port, host=master_address)
        logger.info(f"init process group done for rank {rank}")

    def exposed_init_model(self, inference_config_param: dict, model_or_path: Union[nn.Module, str], model_policy_param: str = None):

        assert dist.is_initialized(), "invoke init_dist_env first please!"

        self.inference_config = InferenceConfig.from_rpc_param(inference_config_param)
        model_policy = _supported_model_policies[model_policy_param]() if model_policy_param else None

        self.dtype = self.inference_config.dtype
        self.verbose = True

        self._init_model(model_or_path, model_policy)
        self._init_fd_tensor()
        self._init_output_tensor()
        logger.info(f"init model done for rank {dist.get_rank()}")

    def exposed_init_cache(self, alloc_shape: Tuple[int, int, int, int], num_layers: int):
        # NOTE(@runyu) move the request_handler logic there
        """Initialize the physical cache on the device.

        For each layer of the model, we allocate two tensors for key and value respectively,
        with shape of [num_blocks, num_kv_heads, block_size, head_size]
        """
        self.k_cache: List[torch.Tensor] = []
        self.v_cache: List[torch.Tensor] = []
        for _ in range(num_layers):
            self.k_cache.append(torch.zeros(alloc_shape, dtype=self.dtype, device=get_accelerator().get_current_device()))
            self.v_cache.append(torch.zeros(alloc_shape, dtype=self.dtype, device=get_accelerator().get_current_device()))
        logger.info("physical cache init over")
    
    def exposed_execute_model_forward(self, input_token_ids: List[int], input_meta_data_param: dict):
        input_meta_data = InputMetaData.from_rpc_param(input_meta_data_param)
        input_meta_data.fd_inter_tensor = self.fd_inter_tensor
        # cumsum = input_meta_data.sequence_lengths.cumsum(dim=0)
        logger.info(f"input_meta_data: {input_meta_data}")
        input_token_ids = torch.tensor(input_token_ids, dtype=torch.int, device=self.device)
        logits = self.model(input_token_ids, self.output_tensor[:input_meta_data.batch_size], input_meta_data, self.k_cache, self.v_cache)
        logits = logits.tolist()
        return logits
    
    def _init_output_tensor(self):
        alloc_shape = (self.inference_config.max_batch_size, self.model_config.hidden_size)
        self.output_tensor = torch.zeros(alloc_shape, dtype=self.dtype, device=self.device)

    def _init_fd_tensor(self):
        fd_inter_tensor = FDIntermTensors()

        if fd_inter_tensor._tensors_initialized:
            fd_inter_tensor._reset()

        # For Spec-Dec, process the speculated tokens plus the token in the last step for each seq
        max_n_tokens = self.inference_config.max_batch_size
        max_n_tokens *= self.inference_config.max_n_spec_tokens + 1

        inference_config = self.inference_config
        kv_max_split_num = (
            inference_config.max_input_len + inference_config.max_output_len + inference_config.block_size - 1
        ) // inference_config.block_size
        head_dim = self.model_config.hidden_size // self.model_config.num_attention_heads

        fd_inter_tensor.initialize(
            max_batch_size=max_n_tokens,
            num_attn_heads=self.model_config.num_attention_heads // self.inference_config.tp_size,
            kv_max_split_num=kv_max_split_num,
            head_dim=head_dim,
            dtype=self.dtype,
            device=get_accelerator().get_current_device(),
        )

        self.fd_inter_tensor = fd_inter_tensor
   
    def _init_model(self, model_or_path: Union[nn.Module, str], model_policy: Policy = None):
        """
        Shard model or/and Load weight

        Args:
            model_or_path Union[nn.Module, str]: path to the checkpoint or model of transformer format.
            model_policy (Policy): the policy to replace the model
        """

        if isinstance(model_or_path, str):
            try:
                hf_config = AutoConfig.from_pretrained(model_or_path, trust_remote_code=True)
                arch = getattr(hf_config, "architectures")[0]
                model = _supported_models[arch](hf_config)
            except Exception as e:
                logger.error(
                    f"An exception occurred during loading model: {e}, model should be loaded by transformers\n"
                )
        else:
            model = model_or_path

        self.model_config = model.config

        torch.cuda.empty_cache()
        init_gpu_memory = torch.cuda.mem_get_info()[0]

        self.device = get_accelerator().get_current_device()
        torch.cuda.set_device(self.device)
        if self.verbose:
            logger.info(f"the device is {self.device}")

        model = model.to(dtype=self.dtype, non_blocking=False).eval()

        if self.verbose:
            logger.info(
                f"Before the shard, Rank: [{dist.get_rank()}], model size: {get_model_size(model)} GB, model's device is: {model.device}"
            )

        if model_policy is None:
            if self.inference_config.pad_input:
                model_type = "padding_" + self.model_config.model_type
            else:
                model_type = "nopadding_" + self.model_config.model_type
            model_policy = model_policy_map[model_type]()

        logger.info(self.inference_config.tp_size)
        pg_mesh = ProcessGroupMesh(self.inference_config.pp_size, self.inference_config.tp_size)
        tp_group = pg_mesh.get_group_along_axis(TP_AXIS)

        model.__setattr__ = setattr
        # model._rpyc_setattr = setattr
        self.model = self._shardformer(
            model,
            model_policy,
            None,
            tp_group=tp_group,
        )

        self.model = ModelWrapper(model).to(device=get_accelerator().get_current_device())

        if self.verbose:
            logger.info(
                f"After the shard, Rank: [{dist.get_rank()}], model size: {get_model_size(self.model)} GB, model's device is: {model.device}"
            )

        # NOTE @runyu add if transformer-remote-url
        # if isinstance(model_or_path, str):
        #     from colossalai.inference.core.plugin import InferCheckpoint_io

        #     cpt_io = InferCheckpoint_io()
        #     if_has_index_file, model_index_file = has_index_file(model_or_path)
        #     assert if_has_index_file, "the model path is invalid"
        #     cpt_io.load_model(self.model, model_index_file)

        free_gpu_memory, total_gpu_memory = torch.cuda.mem_get_info()
        peak_memory = init_gpu_memory - free_gpu_memory
        if self.verbose:
            logger.info(
                f"Rank [{dist.get_rank()}], Model Weight Max Occupy {peak_memory / (1024 ** 3)} GB, Model size: {get_model_size(self.model)} GB"
            )

    def _shardformer(
        self,
        model: nn.Module,
        model_policy: Policy,
        stage_manager: PipelineStageManager = None,
        tp_group: ProcessGroupMesh = None,
    ) -> nn.Module:
        """
        Initialize ShardConfig and replace the model with shardformer.

        Args:
            model (nn.Module): Path or nn.Module of this model.
            model_policy (Policy): The policy to shardformer model which is determined by the model type.
            stage_manager (PipelineStageManager, optional): Used to manage pipeline stages. Defaults to None.
            tp_group (ProcessGroupMesh, optional): Used to manage the process TP group mesh. Defaults to None.

        Returns:
            nn.Module: The model optimized by Shardformer.
        """

        shardconfig = ShardConfig(
            tensor_parallel_process_group=tp_group,
            pipeline_stage_manager=stage_manager,
            enable_tensor_parallelism=(self.inference_config.tp_size > 1),
            enable_fused_normalization=False,
            enable_all_optimization=False,
            enable_flash_attention=False,
            enable_jit_fused=False,
            enable_sequence_parallelism=False,
        )
        shardformer = ShardFormer(shard_config=shardconfig)
        shard_model, _ = shardformer.optimize(model, model_policy)
        return shard_model

    def exposed_compute_only_for_test(self):
        dist_rank = dist.get_rank()

        # Dummy data for each worker
        data = torch.tensor([dist_rank], dtype=torch.float).cuda(dist_rank)
        dist.barrier()

        # Perform distributed all_reduce
        dist.all_reduce(data, op=dist.ReduceOp.SUM)

        dist.barrier()
        logger.info(f"Worker rank {dist_rank}: Sum after all_reduce: {data.item()}")

        return data.item()