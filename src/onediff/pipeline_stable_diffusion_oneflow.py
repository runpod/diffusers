import inspect
from typing import Callable, List, Optional, Union
import oneflow as flow


def is_accelerate_available():
    return False


from packaging import version
from transformers import CLIPFeatureExtractor, CLIPTextModel, CLIPTokenizer

from diffusers.configuration_utils import FrozenDict
from diffusers.models import AutoencoderKL, UNet2DConditionModel
from diffusers.pipeline_utils import DiffusionPipeline
from diffusers.schedulers import (
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
)
from diffusers.utils import deprecate, logging
from diffusers.loaders import LoraLoaderMixin, TextualInversionLoaderMixin
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker,
)

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

from timeit import default_timer as timer
import os
import oneflow as flow
from .graph_utils import GraphCacheMixin


class OneFlowStableDiffusionPipeline(DiffusionPipeline, GraphCacheMixin, TextualInversionLoaderMixin, LoraLoaderMixin):
    r"""
    Pipeline for text-to-image generation using Stable Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder. Stable Diffusion uses the text portion of
            [CLIP](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), specifically
            the [clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14) variant.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class
            [CLIPTokenizer](https://huggingface.co/docs/transformers/v4.21.0/en/model_doc/clip#transformers.CLIPTokenizer).
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please, refer to the [model card](https://huggingface.co/runwayml/stable-diffusion-v1-5) for details.
        feature_extractor ([`CLIPFeatureExtractor`]):
            Model that extracts features from generated images to be used as inputs for the `safety_checker`.
    """
    _optional_components = ["safety_checker", "feature_extractor"]

    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: Union[
            DDIMScheduler,
            PNDMScheduler,
            LMSDiscreteScheduler,
            EulerDiscreteScheduler,
            EulerAncestralDiscreteScheduler,
            DPMSolverMultistepScheduler,
        ],
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPFeatureExtractor,
        requires_safety_checker: bool = True,
    ):
        os.environ["ONEFLOW_MLIR_CSE"] = "1"
        os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
        os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
        os.environ["ONEFLOW_MLIR_FUSE_FORWARD_OPS"] = "1"
        os.environ["ONEFLOW_MLIR_FUSE_OPS_WITH_BACKWARD_IMPL"] = "1"
        os.environ["ONEFLOW_MLIR_GROUP_MATMUL"] = "1"
        os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"

        os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_CONV_BIAS"] = "1"
        os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"

        os.environ["ONEFLOW_KERNEL_CONV_CUTLASS_IMPL_ENABLE_TUNING_WARMUP"] = "1"
        os.environ["ONEFLOW_KERENL_CONV_ENABLE_CUTLASS_IMPL"] = "1"
        # NOTE: avoid overflow
        if "upcast_attention" in unet.config and unet.config.upcast_attention:
            os.environ["ONEFLOW_KERENL_FMHA_ENABLE_TRT_FLASH_ATTN_IMPL"] = "0"
        else:
            os.environ["ONEFLOW_KERENL_FMHA_ENABLE_TRT_FLASH_ATTN_IMPL"] = "1"
        os.environ["ONEFLOW_KERNEL_GLU_ENABLE_DUAL_GEMM_IMPL"] = "1"

        os.environ["ONEFLOW_CONV_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"
        os.environ["ONEFLOW_MATMUL_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"

        super().__init__()

        if (
            hasattr(scheduler.config, "steps_offset")
            and scheduler.config.steps_offset != 1
        ):
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate(
                "steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False
            )
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if (
            hasattr(scheduler.config, "clip_sample")
            and scheduler.config.clip_sample is True
        ):
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
                " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
                " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
                " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
                " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
            )
            deprecate(
                "clip_sample not set", "1.0.0", deprecation_message, standard_warn=False
            )
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        if safety_checker is None and requires_safety_checker:
            logger.warning(
                f"You have disabled the safety checker for {self.__class__} by passing `safety_checker=None`. Ensure"
                " that you abide to the conditions of the Stable Diffusion license and do not expose unfiltered"
                " results in services or applications open to the public. Both the diffusers team and Hugging Face"
                " strongly recommend to keep the safety filter enabled in all public facing circumstances, disabling"
                " it only for use-cases that involve analyzing network behavior or auditing its results. For more"
                " information, please have a look at https://github.com/huggingface/diffusers/pull/254 ."
            )

        if safety_checker is not None and feature_extractor is None:
            raise ValueError(
                "Make sure to define a feature extractor when loading {self.__class__} if you want to use the safety"
                " checker. If you do not want to use the safety checker, you can pass `'safety_checker=None'` instead."
            )

        is_unet_version_less_0_9_0 = hasattr(
            unet.config, "_diffusers_version"
        ) and version.parse(
            version.parse(unet.config._diffusers_version).base_version
        ) < version.parse(
            "0.9.0.dev0"
        )
        is_unet_sample_size_less_64 = (
            hasattr(unet.config, "sample_size") and unet.config.sample_size < 64
        )
        if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
            deprecation_message = (
                "The configuration file of the unet has set the default `sample_size` to smaller than"
                " 64 which seems highly unlikely .If you're checkpoint is a fine-tuned version of any of the"
                " following: \n- CompVis/stable-diffusion-v1-4 \n- CompVis/stable-diffusion-v1-3 \n-"
                " CompVis/stable-diffusion-v1-2 \n- CompVis/stable-diffusion-v1-1 \n- runwayml/stable-diffusion-v1-5"
                " \n- runwayml/stable-diffusion-inpainting \n you should change 'sample_size' to 64 in the"
                " configuration file. Please make sure to update the config accordingly as leaving `sample_size=32`"
                " in the config might lead to incorrect results in future versions. If you have downloaded this"
                " checkpoint from the Hugging Face Hub, it would be very nice if you could open a Pull request for"
                " the `unet/config.json` file"
            )
            deprecate(
                "sample_size<64", "1.0.0", deprecation_message, standard_warn=False
            )
            new_config = dict(unet.config)
            new_config["sample_size"] = 64
            unet._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.register_to_config(requires_safety_checker=requires_safety_checker)

    def enable_xformers_memory_efficient_attention(self):
        r"""
        Enable memory efficient attention as implemented in xformers.

        When this option is enabled, you should observe lower GPU memory usage and a potential speed up at inference
        time. Speed up at training time is not guaranteed.

        Warning: When Memory Efficient Attention and Sliced attention are both enabled, the Memory Efficient Attention
        is used.
        """
        self.unet.set_use_memory_efficient_attention_xformers(True)

    def disable_xformers_memory_efficient_attention(self):
        r"""
        Disable memory efficient attention as implemented in xformers.
        """
        self.unet.set_use_memory_efficient_attention_xformers(False)

    def enable_attention_slicing(self, slice_size: Optional[Union[str, int]] = "auto"):
        r"""
        Enable sliced attention computation.

        When this option is enabled, the attention module will split the input tensor in slices, to compute attention
        in several steps. This is useful to save some memory in exchange for a small speed decrease.

        Args:
            slice_size (`str` or `int`, *optional*, defaults to `"auto"`):
                When `"auto"`, halves the input to the attention heads, so attention will be computed in two steps. If
                a number is provided, uses as many slices as `attention_head_dim // slice_size`. In this case,
                `attention_head_dim` must be a multiple of `slice_size`.
        """
        if slice_size == "auto":
            if isinstance(self.unet.config.attention_head_dim, int):
                # half the attention head size is usually a good trade-off between
                # speed and memory
                slice_size = self.unet.config.attention_head_dim // 2
            else:
                # if `attention_head_dim` is a list, take the smallest head size
                slice_size = min(self.unet.config.attention_head_dim)

        self.unet.set_attention_slice(slice_size)

    def disable_attention_slicing(self):
        r"""
        Disable sliced attention computation. If `enable_attention_slicing` was previously invoked, this method will go
        back to computing attention in one step.
        """
        # set slice_size = `None` to disable `attention slicing`
        self.enable_attention_slicing(None)

    def enable_vae_slicing(self):
        r"""
        Enable sliced VAE decoding.

        When this option is enabled, the VAE will split the input tensor in slices to compute decoding in several
        steps. This is useful to save some memory and allow larger batch sizes.
        """
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        r"""
        Disable sliced VAE decoding. If `enable_vae_slicing` was previously invoked, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        r"""
        Offloads all models to CPU using accelerate, significantly reducing memory usage. When called, unet,
        text_encoder, vae and safety checker have their state dicts saved to CPU and then are moved to a
        `flow.device('meta') and loaded to GPU only when their specific submodule has its `forward` method called.
        """
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError("Please install accelerate via `pip install accelerate`")

        device = flow.device(f"cuda:{gpu_id}")

        for cpu_offloaded_model in [self.unet, self.text_encoder, self.vae]:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

        if self.safety_checker is not None:
            # TODO(Patrick) - there is currently a bug with cpu offload of nn.Parameter in accelerate
            # fix by only offloading self.safety_checker for now
            cpu_offload(self.safety_checker.vision_model, device)

    @property
    def _execution_device(self):
        r"""
        Returns the device on which the pipeline's models will be executed. After calling
        `pipeline.enable_sequential_cpu_offload()` the execution device can only be inferred from Accelerate's module
        hooks.
        """
        if not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                hasattr(module, "_hf_hook")
                and hasattr(module._hf_hook, "execution_device")
                and module._hf_hook.execution_device is not None
            ):
                return flow.device(module._hf_hook.execution_device)
        return self.device

    def _encode_prompt(
        self,
        prompt,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt,
    ):
        r"""
        Encodes the prompt into text encoder hidden states.

        Args:
            prompt (`str` or `list(int)`):
                prompt to be encoded
            device: (`flow.device`):
                flow device
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
        """
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="np",
        )
        text_input_ids = text_inputs.input_ids
        text_input_ids = flow.from_numpy(text_input_ids)
        untruncated_ids = self.tokenizer(
            prompt, padding="max_length", return_tensors="pt"
        ).input_ids

        if (
            text_input_ids.shape == untruncated_ids.shape
            and text_input_ids.numel() == untruncated_ids.numel()
            and not flow.equal(text_input_ids, untruncated_ids)
        ):
            removed_text = self.tokenizer.batch_decode(
                untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1]
            )
            logger.warning(
                "The following part of your input was truncated because CLIP can only handle sequences up to"
                f" {self.tokenizer.model_max_length} tokens: {removed_text}"
            )

        if (
            hasattr(self.text_encoder.config, "use_attention_mask")
            and self.text_encoder.config.use_attention_mask
        ):
            attention_mask = text_inputs.attention_mask.to(device)
        else:
            attention_mask = None

        text_embeddings = self.text_encoder(
            text_input_ids.to(device), attention_mask=attention_mask
        )
        text_embeddings = text_embeddings[0]

        # duplicate text embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_images_per_prompt, 1)
        text_embeddings = text_embeddings.view(
            bs_embed * num_images_per_prompt, seq_len, -1
        )

        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                    f" {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                    f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                    " the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt

            max_length = text_input_ids.shape[-1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="np",
            )

            if (
                hasattr(self.text_encoder.config, "use_attention_mask")
                and self.text_encoder.config.use_attention_mask
            ):
                attention_mask = flow.from_numpy(uncond_input.attention_mask).to(device)
            else:
                attention_mask = None

            uncond_embeddings = self.text_encoder(
                flow.from_numpy(uncond_input.input_ids).to(device),
                attention_mask=attention_mask,
            )
            uncond_embeddings = uncond_embeddings[0]

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = uncond_embeddings.repeat(1, num_images_per_prompt, 1)
            uncond_embeddings = uncond_embeddings.view(
                batch_size * num_images_per_prompt, seq_len, -1
            )

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = flow.cat([uncond_embeddings, text_embeddings])

        return text_embeddings

    def run_safety_checker(self, image, device, dtype):
        if self.safety_checker is not None:
            safety_checker_input = self.feature_extractor(
                self.numpy_to_pil(image), return_tensors="np"
            )
            safety_checker_input.pixel_values = flow.from_numpy(
                safety_checker_input.pixel_values
            ).to(device)
            image, has_nsfw_concept = self.safety_checker(
                images=image, clip_input=safety_checker_input.pixel_values.to(dtype)
            )
        else:
            has_nsfw_concept = None
        return image, has_nsfw_concept

    def decode_latents(self, latents):
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample
        image = (image / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloa16
        image = image.cpu().permute(0, 2, 3, 1).float().numpy()
        return image

    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (?) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to ? in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]

        accepts_eta = "eta" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(self, prompt, height, width, callback_steps):
        if not isinstance(prompt, str) and not isinstance(prompt, list):
            raise ValueError(
                f"`prompt` has to be of type `str` or `list` but is {type(prompt)}"
            )

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f"`height` and `width` have to be divisible by 8 but are {height} and {width}."
            )

        if (callback_steps is None) or (
            callback_steps is not None
            and (not isinstance(callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )

    def prepare_latents(
        self,
        batch_size,
        num_channels_latents,
        height,
        width,
        dtype,
        device,
        generator,
        latents=None,
    ):
        shape = (
            batch_size,
            num_channels_latents,
            height // self.vae_scale_factor,
            width // self.vae_scale_factor,
        )
        if latents is None:
            if device.type == "mps":
                # randn does not work reproducibly on mps
                latents = flow.randn(
                    shape, generator=generator, device="cpu", dtype=dtype
                ).to(device)
            else:
                latents = flow.randn(
                    shape, generator=generator, device=device, dtype=dtype
                )
        else:
            if latents.shape != shape:
                raise ValueError(
                    f"Unexpected latents shape, got {latents.shape}, expected {shape}"
                )
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    def set_unet_graphs_cache_size(self, cache_size: int):
        r"""
        Set the cache size of compiled unet graphs.
        This option is designed to control the GPU memory size.
        Args:
            cache_size ([`int`]):
                New cache size, i.e., the maximum number of unet graphs.
        """
        logger.warning(
            f"`set_unet_graphs_cache_size` is deprecated, please use `set_graph_compile_cache_size` instead."
        )
        self.set_graph_compile_cache_size(cache_size)

    @flow.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: float = 0.0,
        generator: Optional[flow.Generator] = None,
        latents: Optional[flow.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, flow.FloatTensor], None]] = None,
        callback_steps: Optional[int] = 1,
        compile_unet: bool = True,
        compile_vae: bool = True,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            height (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (?) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`flow.Generator`, *optional*):
                A [flow generator](https://pytorch.org/docs/stable/generated/flow.Generator.html) to make generation
                deterministic.
            latents (`flow.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that will be called every `callback_steps` steps during inference. The function will be
                called with the following arguments: `callback(step: int, timestep: int, latents: flow.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function will be called. If not specified, the callback will be
                called at every step.

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(prompt, height, width, callback_steps)

        # 2. Define call parameters
        batch_size = 1 if isinstance(prompt, str) else len(prompt)
        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        # 3. Encode input prompt
        text_embeddings = self._encode_prompt(
            prompt,
            device,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
        )

        # 4. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps

        # 5. Prepare latent variables
        num_channels_latents = self.unet.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            height,
            width,
            text_embeddings.dtype,
            device,
            generator,
            latents,
        )

        # compile vae graph
        if compile_vae:
            vae_post_process_graph = self.get_graph("vae", self.vae)
            if vae_post_process_graph.is_compiled is False:
                vae_post_process_graph._compile(latents)

        # compile unet graph
        if compile_unet:
            unet_graph = self.get_graph("unet", self.unet)
            if unet_graph.is_compiled is False:
                latent_model_input = (
                    flow.cat([latents] * 2) if do_classifier_free_guidance else latents
                )
                _, t = list(enumerate(self.scheduler.timesteps))[0]
                unet_graph._compile(latent_model_input, t, text_embeddings)
        # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        # 7. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                # expand the latents if we are doing classifier free guidance
                latent_model_input = (
                    flow.cat([latents] * 2) if do_classifier_free_guidance else latents
                )
                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t
                )

                # predict the noise residual
                if compile_unet:
                    flow._oneflow_internal.profiler.RangePush(f"denoise-{i}-unet-graph")
                    noise_pred = unet_graph(latent_model_input, t, text_embeddings)
                    flow._oneflow_internal.profiler.RangePop()
                else:
                    noise_pred = self.unet(
                        latent_model_input, t, encoder_hidden_states=text_embeddings
                    ).sample

                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                    noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond
                    )

                    # compute the previous noisy sample x_t -> x_t-1
                    latents = self.scheduler.step(
                        noise_pred, t, latents, **extra_step_kwargs
                    ).prev_sample

                # call the callback, if provided
                if (i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0:
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)

        # 8. Post-processing
        if compile_vae:
            image = vae_post_process_graph(latents)
            image = image.cpu().permute(0, 2, 3, 1).float().numpy()
        else:
            image = self.decode_latents(latents)

        # 9. Run safety checker
        image, has_nsfw_concept = self.run_safety_checker(
            image, device, text_embeddings.dtype
        )

        # 10. Convert to PIL
        if output_type == "pil":
            image = self.numpy_to_pil(image)

        if not return_dict:
            return (image, has_nsfw_concept)

        return StableDiffusionPipelineOutput(
            images=image, nsfw_content_detected=has_nsfw_concept
        )

    def load_fast_pipeline(storage_folder):
        import gc
        import diffusers
        import pickle
        import os

        # disable garbage collector
        gc.disable()

        def seq_to_func_return(seq, need_unpack=False):
            if need_unpack:
                return seq[0]
            return seq

        def load(filename):
            filepath = os.path.join(storage_folder, '{}.pkl'.format(filename))
            # Open a file in binary mode
            with open(filepath, 'rb') as file:
                # disable garbage collector
                gc.disable()

                # Pickle dump the object to the file
                dump = pickle.load(file)

                # enable garbage collector again
                gc.enable()

                return dump

        flow.mock_torch.enable()
        previous_init = OneFlowStableDiffusionPipeline.__init__

        def hijacked_init(self,
            vae,
            text_encoder,
            tokenizer,
            unet,
            scheduler,
            safety_checker,
            feature_extractor,
            requires_safety_checker=False):
                unet = load('unet')
                vae = load('vae')
                text_encoder = load('text_encoder')
                safety_checker = None

                return previous_init(self,
                    vae,
                    text_encoder,
                    tokenizer,
                    unet,
                    scheduler,
                    safety_checker,
                    feature_extractor,
                    requires_safety_checker=False
                )

        OneFlowStableDiffusionPipeline.__init__ = hijacked_init

        previous_load_sub_model = diffusers.pipelines.pipeline_utils.load_sub_model
        def hijacked_load_sub_model(*args, **kwargs):
            if kwargs['name'] == 'unet' or kwargs['name'] == 'vae' or kwargs['name'] == 'text_encoder' or kwargs['name'] == 'safety_checker':
                return None
            return previous_load_sub_model(*args, **kwargs)

        diffusers.pipelines.pipeline_utils.load_sub_model = hijacked_load_sub_model

        pipe = OneFlowStableDiffusionPipeline.from_pretrained(
            "CompVis/stable-diffusion-v1-4",
            use_auth_token=True,
            revision="fp16",
            torch_dtype=flow.float16,
        )

        pipe = pipe.to("cuda")

        unet_graph = pipe.get_graph("unet", pipe.unet)
        vae_graph = pipe.get_graph("vae", pipe.vae)

        def create_hijack_compile(self, model):
            def hijack_compile(*args, **kwargs):
                # disable garbage collector
                gc.disable()

                state_dict = load('{}_compiled'.format(model))

                # enable garbage collector again
                gc.enable()
            
                self.load_runtime_state_dict(state_dict)
                return (self._full_job_proto, seq_to_func_return(self._eager_outputs_buffer[0], True),)
            return hijack_compile

        unet_graph._compile_new = create_hijack_compile(unet_graph, 'unet')
        vae_graph._compile_new = create_hijack_compile(vae_graph, 'vae')

        # enable garbage collector again
        gc.enable()

        return pipe

    def precompile_and_save_static_graph(storage_folder):
        import gc
        import pickle
        import os

        gc.disable()
        flow.mock_torch.enable()

        def save(filename, data):
            # Create the directory and its parents if they don't exist
            filepath = os.path.join(storage_folder, '{}.pkl'.format(filename))
            # Extract the directory path
            directory_path = os.path.dirname(filepath)
            # Create the directories for the folder of the file path
            os.makedirs(directory_path, exist_ok=True)
            # Open a file in binary mode
            with open(filepath, 'wb') as file:
                # Pickle dump the object to the file
                pickle.dump(data, file)

        pipe = OneFlowStableDiffusionPipeline.from_pretrained(
            "CompVis/stable-diffusion-v1-4",
            use_auth_token=True,
            revision="fp16",
            torch_dtype=flow.float16,
        )

        # Save static graphs
        save('unet', pipe.unet)
        save('vae', pipe.vae)
        save('text_encoder', pipe.text_encoder)
        save('safety_checker', pipe.safety_checker)

        # Override the ._compile of each below before doing the __call__ on pipe
        unet_graph = pipe.get_graph("unet", pipe.unet)
        vae_graph = pipe.get_graph("vae", pipe.vae)

        def create_hijack_compile(self, model):
            graph_compilation_func = self._compile_new
            def hijack_compile(*args, **kwargs):
                # Call the graph compilation
                print("Hijacking the compile for model {}".format(model))
                eager_output = graph_compilation_func(*args, **kwargs)
                state_dict = self.runtime_state_dict()
                save('{}_compiled'.format(model), state_dict)
                return eager_output
            return hijack_compile

        unet_graph._compile_new = create_hijack_compile(unet_graph, 'unet')
        vae_graph._compile_new = create_hijack_compile(vae_graph, 'vae')

        # Perform forward pass
        pipe = pipe.to("cuda")
        pipe("Unraveling Time: Depict a scene where time appears to be flowing in reverse, unraveling the fabric of reality.")
