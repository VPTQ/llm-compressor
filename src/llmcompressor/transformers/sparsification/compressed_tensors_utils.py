import re
import weakref
from functools import reduce, wraps
from typing import Dict, Optional

import torch
import transformers
from accelerate.accelerator import get_state_dict_offloaded_model
from compressed_tensors import ModelCompressor, SparsityCompressionConfig
from loguru import logger
from safetensors.torch import _find_shared_tensors
from transformers import PreTrainedModel

from llmcompressor.transformers.compression.quantization_format import (
    infer_quantization_format,
)
from llmcompressor.transformers.compression.sparsity_config import (
    SparsityConfigMetadata,
)

__all__ = ["modify_save_pretrained"]


def modify_save_pretrained(model: PreTrainedModel):
    """
    Overrides a PreTrainedModel's save_pretrained() method with a wrapped version that
    supports compression
    """

    def save_pretrained_compressed(save_pretrained_method):
        if getattr(save_pretrained_method, "_overridden", False):
            # `model.save_pretrained` has already been replaced, return.
            return save_pretrained_method

        # Keep a weak reference to the model class and unbound save_pretrained
        # method so we can call the original
        model_ref = weakref.ref(save_pretrained_method.__self__)
        original_save_pretrained = save_pretrained_method.__func__
        model_class = model_ref().__class__
        del save_pretrained_method

        @wraps(original_save_pretrained)
        def save_pretrained_wrapper(
            save_directory: str,
            sparsity_config: Optional[SparsityCompressionConfig] = None,
            quantization_format: Optional[str] = None,
            save_compressed: bool = True,
            skip_compression_stats: bool = False,
            state_dict: Optional[Dict[str, torch.Tensor]] = None,
            **kwargs,
        ):
            """
            Wrapper around PreTrainedModel.save_pretrained(), adds functionality for
            saving models in a compressed format on disk. The compression format is
            saved to the model's config file

            :param save_directory: output directory to save model to
            :param sparsity_config: optional sparsity config to compress model with,
            if no config is provided it will be inferred from the model
            :param quantization_format: optional compression format for quantized
            models. If none is provided it will be inferred from the model
            :param save_compressed: whether or not to compress the model on disk
            :param skip_compression_stats: whether to skip the calculation of
            compression statistics (such as global sparsity and sparsity structure) when
            saving a model in dense format. This also means that sparsity config
            cannot be inferred
            :param state_dict: TODO state_dict gets passed in as a kwarg for FSDP models
            :param kwargs: additional kwargs to pass on to model.save_pretrained
            """

            # HACK: Override the dtype_byte_size function in transformers to
            # support float8 types. Fix is posted upstream
            # https://github.com/huggingface/transformers/pull/30488
            transformers.modeling_utils.dtype_byte_size = new_dtype_byte_size

            model = model_ref()
            if state_dict is None:
                state_dict = get_state_dict_offloaded_model(model)

            if not save_compressed:
                original_save_pretrained.__get__(model, model_class)(
                    save_directory, state_dict=state_dict, **kwargs
                )
                return

            # use given sparsity config
            if sparsity_config is not None:
                sparsity_config.global_sparsity = (
                    SparsityConfigMetadata.infer_global_sparsity(
                        model, state_dict=state_dict
                    )
                )
                sparsity_config.sparsity_structure = (
                    SparsityConfigMetadata.infer_sparsity_structure()
                )

            # attempt to infer sparsity config
            elif not skip_compression_stats:
                # try to infer a sparsity config from the model if none is provided
                logger.info(
                    "Inferring a sparsity configuration requires a global sparsity "
                    "calculation. This can be costly for large models. To skip the "
                    "calculation of compression statistics set "
                    "skip_compression_stats=True"
                )
                sparsity_config = SparsityConfigMetadata.from_pretrained(
                    model, state_dict=state_dict, compress=True
                )
            else:
                logger.warning(
                    "Sparsity configuration cannot be inferred if skip_compression_stats=True"
                )

            # use given quantization format or infer
            if quantization_format is None:
                quantization_format = infer_quantization_format(
                    model=model,
                    sparsity_config=sparsity_config,
                )

            compressor = ModelCompressor.from_pretrained_model(
                model,
                sparsity_config=sparsity_config,
                quantization_format=quantization_format,
            )

            if compressor is None:
                # model is not compressed or quantized, save as normal
                original_save_pretrained.__get__(model, model_class)(
                    save_directory, state_dict=state_dict, **kwargs
                )
                return

            # make sure we're on the main process when saving
            if state_dict is not None and len(state_dict) > 0:
                compressed_state_dict = compressor.compress(model, state_dict)

                kwargs["safe_serialization"] = kwargs.get("safe_serialization", True)
                original_save_pretrained.__get__(model, model_class)(
                    save_directory, state_dict=compressed_state_dict, **kwargs
                )
                compressor.update_config(save_directory)

        save_pretrained_wrapper._overriden = True
        return save_pretrained_wrapper

    # wrap save_pretrained
    model.save_pretrained = save_pretrained_compressed(model.save_pretrained)


# HACK: Override the dtype_byte_size function in transformers to support float8 types
# Fix is posted upstream https://github.com/huggingface/transformers/pull/30488
def new_dtype_byte_size(dtype):
    if dtype == torch.bool:
        return 1 / 8
    bit_search = re.search(r"[^\d](\d+)_?", str(dtype))
    if bit_search is None:
        raise ValueError(f"`dtype` is not a valid dtype: {dtype}.")
    bit_size = int(bit_search.groups()[0])
    return bit_size // 8


def patch_tied_tensors_bug(model: torch.nn.Module) -> torch.nn.Module:
    """
    Patches bug where HF transformers will fail to untie weights under specific
    circumstances (https://github.com/huggingface/transformers/issues/33689).

    This function detects those cases and unties the tensors if applicable

    :param model: model to fix
    :return: model with fixed parameters
    """
    if not model.config.tie_word_embeddings:
        tensor_groups = _find_shared_tensors(get_state_dict_offloaded_model(model))
        for tensor_group in tensor_groups:
            if len(tensor_group) > 1:
                if not set(model._tied_weights_keys).intersection(tensor_group):
                    raise ValueError(
                        "Model contains unexpected shared tensors. Expected "
                        f"{model._tied_weights_keys}, found {tensor_group}"
                    )

                for tensor_path in tensor_group:
                    tensor_parts = tensor_path.split(".")
                    parameter = reduce(getattr, tensor_parts, model)
                    parameter.data = parameter.data.clone()

    return model
