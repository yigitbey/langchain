import logging
from typing import Any, Dict, Iterator, List, Optional
from functools import partial
from pydantic import Field, root_validator

from langchain.callbacks.manager import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun
from langchain.llms.base import LLM
from langchain.llms.utils import enforce_stop_tokens
from langchain.schema.output import GenerationChunk

logger = logging.getLogger(__name__)


class LlamaCpp(LLM):
    """llama.cpp model.

    To use, you should have the llama-cpp-python library installed, and provide the
    path to the Llama model as a named parameter to the constructor.
    Check out: https://github.com/abetlen/llama-cpp-python

    Example:
        .. code-block:: python

            from langchain.llms import LlamaCpp
            llm = LlamaCpp(model_path="/path/to/llama/model")
    """

    client: Any  #: :meta private:
    model_path: str
    """The path to the Llama model file."""

    lora_base: Optional[str] = None
    """The path to the Llama LoRA base model."""

    lora_path: Optional[str] = None
    """The path to the Llama LoRA. If None, no LoRa is loaded."""

    n_ctx: int = Field(512, alias="n_ctx")
    """Token context window."""

    n_parts: int = Field(-1, alias="n_parts")
    """Number of parts to split the model into.
    If -1, the number of parts is automatically determined."""

    seed: int = Field(-1, alias="seed")
    """Seed. If -1, a random seed is used."""

    f16_kv: bool = Field(True, alias="f16_kv")
    """Use half-precision for key/value cache."""

    logits_all: bool = Field(False, alias="logits_all")
    """Return logits for all tokens, not just the last token."""

    vocab_only: bool = Field(False, alias="vocab_only")
    """Only load the vocabulary, no weights."""

    use_mlock: bool = Field(False, alias="use_mlock")
    """Force system to keep model in RAM."""

    n_threads: Optional[int] = Field(None, alias="n_threads")
    """Number of threads to use.
    If None, the number of threads is automatically determined."""

    n_batch: Optional[int] = Field(8, alias="n_batch")
    """Number of tokens to process in parallel.
    Should be a number between 1 and n_ctx."""

    n_gpu_layers: Optional[int] = Field(None, alias="n_gpu_layers")
    """Number of layers to be loaded into gpu memory. Default None."""

    n_gqa: Optional[int] = Field(None, alias="n_gqa")
    """GQA required for LLAMA 2 models"""

    suffix: Optional[str] = Field(None)
    """A suffix to append to the generated text. If None, no suffix is appended."""

    max_tokens: Optional[int] = 256
    """The maximum number of tokens to generate."""

    temperature: Optional[float] = 0.8
    """The temperature to use for sampling."""

    top_p: Optional[float] = 0.95
    """The top-p value to use for sampling."""

    logprobs: Optional[int] = Field(None)
    """The number of logprobs to return. If None, no logprobs are returned."""

    echo: Optional[bool] = False
    """Whether to echo the prompt."""

    stop: Optional[List[str]] = []
    """A list of strings to stop generation when encountered."""

    repeat_penalty: Optional[float] = 1.1
    """The penalty to apply to repeated tokens."""

    top_k: Optional[int] = 40
    """The top-k value to use for sampling."""

    last_n_tokens_size: Optional[int] = 64
    """The number of tokens to look back when applying the repeat_penalty."""

    use_mmap: Optional[bool] = True
    """Whether to keep the model loaded in RAM"""

    streaming: bool = True
    """Whether to stream the results, token by token."""

    verbose: bool = True
    """Print verbose output to stderr."""

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that llama-cpp-python library is installed."""
        model_path = values["model_path"]
        model_param_names = [
            "lora_path",
            "lora_base",
            "n_ctx",
            "n_parts",
            "seed",
            "f16_kv",
            "logits_all",
            "vocab_only",
            "use_mlock",
            "n_threads",
            "n_batch",
            "use_mmap",
            "last_n_tokens_size",
            "verbose",
        ]
        model_params = {k: values[k] for k in model_param_names}
        # For backwards compatibility, only include if non-null.
        if values["n_gqa"] is not None:
            model_params["n_gqa"] = values["n_gqa"]
        if values["n_gpu_layers"] is not None:
            model_params["n_gpu_layers"] = values["n_gpu_layers"]

        try:
            from llama_cpp import Llama

            values["client"] = Llama(model_path, **model_params)
        except ImportError:
            raise ImportError(
                "Could not import llama-cpp-python library. "
                "Please install the llama-cpp-python library to "
                "use this embedding model: pip install llama-cpp-python"
            )
        except Exception as e:
            raise ValueError(
                f"Could not load Llama model from path: {model_path}. "
                f"Received error {e}"
            )

        return values

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling llama_cpp."""
        return {
            "suffix": self.suffix,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "logprobs": self.logprobs,
            "echo": self.echo,
            "stop_sequences": self.stop,  # key here is convention among LLM classes
            "repeat_penalty": self.repeat_penalty,
            "top_k": self.top_k,
        }

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {**{"model_path": self.model_path}, **self._default_params}

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "llamacpp"

    def _get_parameters(self, stop: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Performs sanity check, preparing parameters in format needed by llama_cpp.

        Args:
            stop (Optional[List[str]]): List of stop sequences for llama_cpp.

        Returns:
            Dictionary containing the combined parameters.
        """

        # Raise error if stop sequences are in both input and default params
        if self.stop and stop is not None:
            raise ValueError("`stop` found in both the input and default params.")

        params = self._default_params

        # llama_cpp expects the "stop" key not this, so we remove it:
        params.pop("stop_sequences")

        # then sets it as configured, or default to an empty list:
        params["stop"] = self.stop or stop or []

        return params

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call the Llama model and return the output.

        Args:
            prompt: The prompt to use for generation.
            stop: A list of strings to stop generation when encountered.

        Returns:
            The generated text.

        Example:
            .. code-block:: python

                from langchain.llms import LlamaCpp
                llm = LlamaCpp(model_path="/path/to/local/llama/model.bin")
                llm("This is a prompt.")
        """
        if self.streaming:
            # If streaming is enabled, we use the stream
            # method that yields as they are generated
            # and return the combined strings from the first choices's text:
            combined_text_output = ""
            for chunk in self._stream(
                prompt=prompt, stop=stop, run_manager=run_manager, **kwargs
            ):
                combined_text_output += chunk.text
            return combined_text_output
        else:
            params = self._get_parameters(stop)
            params = {**params, **kwargs}
            result = self.client(prompt=prompt, **params)
            return result["choices"][0]["text"]
        
    async def _acall(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        r"""Asynchronous Call out to Llama.cpp's generate method.
        Args:
            prompt: The prompt to pass into the model.
            stop: A list of strings to stop generation when encountered.
        Returns:
            The string generated by the model.
        Example:
            .. code-block:: python
                prompt = "Once upon a time, "
                response = model(prompt, n_predict=55)
        """
        text_callback = None
        if run_manager:
            text_callback = partial(run_manager.on_llm_new_token, verbose=self.verbose)

        combined_text_output = ""

        for token in self.stream(prompt=prompt, stop=stop, run_manager=run_manager):
            combined_text_output += token["choices"][0]["text"]
            if text_callback:
                await text_callback(token)
        if stop is not None:
            combined_text_output = enforce_stop_tokens(text, stop)

        return combined_text_output
    
    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        """Yields results objects as they are generated in real time.

        It also calls the callback manager's on_llm_new_token event with
        similar parameters to the OpenAI LLM class method of the same name.

        Args:
            prompt: The prompts to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            A generator representing the stream of tokens being generated.

        Yields:
            A dictionary like objects containing a string token and metadata.
            See llama-cpp-python docs and below for more.

        Example:
            .. code-block:: python

                from langchain.llms import LlamaCpp
                llm = LlamaCpp(
                    model_path="/path/to/local/model.bin",
                    temperature = 0.5
                )
                for chunk in llm.stream("Ask 'Hi, how are you?' like a pirate:'",
                        stop=["'","\n"]):
                    result = chunk["choices"][0]
                    print(result["text"], end='', flush=True)

        """
        params = {**self._get_parameters(stop), **kwargs}
        result = self.client(prompt=prompt, stream=True, **params)
        for part in result:
            logprobs = part["choices"][0].get("logprobs", None)
            chunk = GenerationChunk(
                text=part["choices"][0]["text"],
                generation_info={"logprobs": logprobs},
            )
            yield chunk
            if run_manager:
                run_manager.on_llm_new_token(
                    token=chunk.text, verbose=self.verbose, log_probs=logprobs
                )

    def get_num_tokens(self, text: str) -> int:
        tokenized_text = self.client.tokenize(text.encode("utf-8"))
        return len(tokenized_text)
