# mypy: disable-error-code="name-defined"
from __future__ import annotations
import asyncio, logging, typing as t
import bentoml, bentoml.client, openllm, httpx
from abc import abstractmethod
from http import HTTPStatus
from urllib.parse import urljoin
from openllm._typing_compat import overload, LiteralString

T = t.TypeVar("T")
T_co = t.TypeVar("T_co", covariant=True)

if t.TYPE_CHECKING:
  import transformers
  from openllm._typing_compat import DictStrAny, LiteralRuntime
else: transformers = openllm.utils.LazyLoader("transformers", globals(), "transformers")

class AnnotatedClient(t.Protocol[T_co]):
  server_url: str
  _svc: bentoml.Service
  endpoints: list[str]
  def health(self, *args: t.Any, **attrs: t.Any) -> t.Any: ...
  async def async_health(self) -> t.Any: ...
  def generate_v1(self, qa: openllm.GenerationInput) -> T_co: ...
  def metadata_v1(self) -> T_co: ...
  def embeddings_v1(self) -> t.Sequence[float]: ...
  def call(self, name: str, *args: t.Any, **attrs: t.Any) -> T_co: ...
  async def async_call(self, name: str, *args: t.Any, **attrs: t.Any) -> T_co: ...
  @staticmethod
  def wait_until_server_ready(host: str, port: int, timeout: float = 30, **kwargs: t.Any) -> None: ...
  @staticmethod
  def from_url(server_url: str) -> AnnotatedClient[t.Any]: ...

logger = logging.getLogger(__name__)

def in_async_context() -> bool:
  try:
    _ = asyncio.get_running_loop()
    return True
  except RuntimeError: return False

class ClientMeta(t.Generic[T]):
  _api_version: str
  _client_type: t.Literal["GrpcClient", "HTTPClient"]
  _host: str
  _port: str

  __client__: AnnotatedClient[T] | None = None
  __agent__: transformers.HfAgent | None = None
  __llm__: openllm.LLM[t.Any, t.Any] | None = None

  def __init__(self, address: str, timeout: int = 30): self._address,self._timeout = address,timeout
  def __init_subclass__(cls, *, client_type: t.Literal["http", "grpc"] = "http", api_version: str = "v1"): cls._client_type, cls._api_version = "HTTPClient" if client_type == "http" else "GrpcClient", api_version
  @property
  def _hf_agent(self) -> transformers.HfAgent:
    if not self.supports_hf_agent: raise openllm.exceptions.OpenLLMException(f"{self.model_name} ({self.framework}) does not support running HF agent.")
    if self.__agent__ is None:
      if not openllm.utils.is_transformers_supports_agent(): raise RuntimeError("Current 'transformers' does not support Agent. Make sure to upgrade to at least 4.29: 'pip install -U \"transformers>=4.29\"'")
      self.__agent__ = transformers.HfAgent(urljoin(self._address, "/hf/agent"))
    return self.__agent__
  @property
  def _metadata(self) -> T: return httpx.post(urljoin(self._address, f"/{self._api_version}/metadata")).json() if in_async_context() else self.call("metadata")
  @property
  @abstractmethod
  def model_name(self) -> str: raise NotImplementedError
  @property
  @abstractmethod
  def framework(self) -> LiteralRuntime: raise NotImplementedError
  @property
  @abstractmethod
  def timeout(self) -> int: raise NotImplementedError
  @property
  @abstractmethod
  def model_id(self) -> str: raise NotImplementedError
  @property
  @abstractmethod
  def configuration(self) -> dict[str, t.Any]: raise NotImplementedError
  @property
  @abstractmethod
  def supports_embeddings(self) -> bool: raise NotImplementedError
  @property
  @abstractmethod
  def supports_hf_agent(self) -> bool: raise NotImplementedError
  @abstractmethod
  def postprocess(self, result: t.Any) -> openllm.GenerationOutput: ...
  @abstractmethod
  def _run_hf_agent(self, *args: t.Any, **kwargs: t.Any) -> t.Any: ...

  @property
  def config(self) -> openllm.LLMConfig: return self.llm.config
  @property
  def llm(self) -> openllm.LLM[t.Any, t.Any]:
    # XXX: if the server runs vllm or any framework that is not available from the user client, client will fail.
    if self.__llm__ is None: self.__llm__ = openllm.infer_auto_class(self.framework).for_model(self.model_name)
    return self.__llm__

  def call(self, name: str, *args: t.Any, **attrs: t.Any) -> T: return self._cached.call(f"{name}_{self._api_version}", *args, **attrs)
  async def acall(self, name: str, *args: t.Any, **attrs: t.Any) -> T: return await self._cached.async_call(f"{name}_{self._api_version}", *args, **attrs)
  @property
  def _cached(self) -> AnnotatedClient[T]:
    client_class = t.cast(AnnotatedClient[T], getattr(bentoml.client, self._client_type))
    if self.__client__ is None:
      client_class.wait_until_server_ready(self._host, int(self._port), timeout=self._timeout)
      self.__client__ = client_class.from_url(self._address)
    return self.__client__

class BaseClient(ClientMeta[T]):
  def health(self) -> t.Any: raise NotImplementedError
  def chat(self, prompt: str, history: list[str], **attrs: t.Any) -> str: raise NotImplementedError
  def embed(self, prompt: t.Sequence[str] | str) -> openllm.EmbeddingsOutput: raise NotImplementedError
  @overload
  def query(self, prompt: str, *, return_response: t.Literal["processed"], **attrs: t.Any) -> str: ...
  @overload
  def query(self, prompt: str, *, return_response: t.Literal["raw"], **attrs: t.Any) -> DictStrAny: ...
  @overload
  def query(self, prompt: str, *, return_response: t.Literal["attrs"], **attrs: t.Any) -> openllm.GenerationOutput: ...
  def query(self, prompt: str, return_response: t.Literal["attrs", "raw", "processed"] = "processed", **attrs: t.Any) -> openllm.GenerationOutput | DictStrAny | str:
    return_raw_response = attrs.pop("return_raw_response", None)
    if return_raw_response is not None:
      logger.warning("'return_raw_response' is now deprecated. Please use 'return_response=\"raw\"' instead.")
      if return_raw_response is True: return_response = "raw"
    return_attrs = attrs.pop("return_attrs", None)
    if return_attrs is not None:
      logger.warning("'return_attrs' is now deprecated. Please use 'return_response=\"attrs\"' instead.")
      if return_attrs is True: return_response = "attrs"
    use_default_prompt_template = attrs.pop("use_default_prompt_template", False)
    prompt, generate_kwargs, postprocess_kwargs = self.llm.sanitize_parameters(prompt, use_default_prompt_template=use_default_prompt_template, **attrs)

    inputs = openllm.GenerationInput(prompt=prompt, llm_config=self.config.model_construct_env(**generate_kwargs))
    if in_async_context(): result = httpx.post(urljoin(self._address, f"/{self._api_version}/generate"), json=inputs.model_dump(), timeout=self.timeout).json()
    else: result = self.call("generate", inputs.model_dump())
    r = self.postprocess(result)
    if return_response == "attrs": return r
    elif return_response == "raw": return openllm.utils.bentoml_cattr.unstructure(r)
    else: return self.llm.postprocess_generate(prompt, r.responses, **postprocess_kwargs)

  # NOTE: Scikit interface
  @overload
  def predict(self, prompt: str, *, return_response: t.Literal["processed"], **attrs: t.Any) -> str: ...
  @overload
  def predict(self, prompt: str, *, return_response: t.Literal["raw"], **attrs: t.Any) -> DictStrAny: ...
  @overload
  def predict(self, prompt: str, *, return_response: t.Literal["attrs"], **attrs: t.Any) -> openllm.GenerationOutput: ...
  def predict(self, prompt: str, **attrs: t.Any) -> openllm.GenerationOutput | DictStrAny | str: return t.cast(t.Union[openllm.GenerationOutput, DictStrAny, str], self.query(prompt, **attrs))

  def ask_agent(self, task: str, *, return_code: bool = False, remote: bool = False, agent_type: LiteralString = "hf", **attrs: t.Any) -> t.Any:
    if agent_type == "hf": return self._run_hf_agent(task, return_code=return_code, remote=remote, **attrs)
    else: raise RuntimeError(f"Unknown 'agent_type={agent_type}'")

  def _run_hf_agent(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
    if len(args) > 1: raise ValueError("'args' should only take one positional argument.")
    task = kwargs.pop("task", args[0])
    return_code = kwargs.pop("return_code", False)
    remote = kwargs.pop("remote", False)
    try: return self._hf_agent.run(task, return_code=return_code, remote=remote, **kwargs)
    except Exception as err:
      logger.error("Exception caught while sending instruction to HF agent: %s", err, exc_info=err)
      logger.info("Tip: LLMServer at '%s' might not support 'generate_one'.", self._address)

class BaseAsyncClient(ClientMeta[T]):
  async def health(self) -> t.Any: raise NotImplementedError
  async def chat(self, prompt: str, history: list[str], **attrs: t.Any) -> str: raise NotImplementedError
  async def embed(self, prompt: t.Sequence[str] | str) -> openllm.EmbeddingsOutput: raise NotImplementedError
  @overload
  async def query(self, prompt: str, *, return_response: t.Literal["processed"], **attrs: t.Any) -> str: ...
  @overload
  async def query(self, prompt: str, *, return_response: t.Literal["raw"], **attrs: t.Any) -> DictStrAny: ...
  @overload
  async def query(self, prompt: str, *, return_response: t.Literal["attrs"], **attrs: t.Any) -> openllm.GenerationOutput: ...
  async def query(self, prompt: str, return_response: t.Literal["attrs", "raw", "processed"] = "processed", **attrs: t.Any) -> openllm.GenerationOutput | DictStrAny | str:
    return_raw_response = attrs.pop("return_raw_response", None)
    if return_raw_response is not None:
      logger.warning("'return_raw_response' is now deprecated. Please use 'return_response=\"raw\"' instead.")
      if return_raw_response is True: return_response = "raw"
    return_attrs = attrs.pop("return_attrs", None)
    if return_attrs is not None:
      logger.warning("'return_attrs' is now deprecated. Please use 'return_response=\"attrs\"' instead.")
      if return_attrs is True: return_response = "attrs"
    use_default_prompt_template = attrs.pop("use_default_prompt_template", False)
    prompt, generate_kwargs, postprocess_kwargs = self.llm.sanitize_parameters(prompt, use_default_prompt_template=use_default_prompt_template, **attrs)

    inputs = openllm.GenerationInput(prompt=prompt, llm_config=self.config.model_construct_env(**generate_kwargs))
    res = await self.acall("generate", inputs.model_dump())
    r = self.postprocess(res)

    if return_response == "attrs": return r
    elif return_response == "raw": return openllm.utils.bentoml_cattr.unstructure(r)
    else: return self.llm.postprocess_generate(prompt, r.responses, **postprocess_kwargs)

  # NOTE: Scikit interface
  @overload
  async def predict(self, prompt: str, *, return_response: t.Literal["processed"], **attrs: t.Any) -> str: ...
  @overload
  async def predict(self, prompt: str, *, return_response: t.Literal["raw"], **attrs: t.Any) -> DictStrAny: ...
  @overload
  async def predict(self, prompt: str, *, return_response: t.Literal["attrs"], **attrs: t.Any) -> openllm.GenerationOutput: ...
  async def predict(self, prompt: str, **attrs: t.Any) -> openllm.GenerationOutput | DictStrAny | str: return t.cast(t.Union[openllm.GenerationOutput, DictStrAny, str], await self.query(prompt, **attrs))
  async def ask_agent(self, task: str, *, return_code: bool = False, remote: bool = False, agent_type: LiteralString = "hf", **attrs: t.Any) -> t.Any:
    """Async version of agent.run."""
    if agent_type == "hf": return await self._run_hf_agent(task, return_code=return_code, remote=remote, **attrs)
    else: raise RuntimeError(f"Unknown 'agent_type={agent_type}'")
  async def _run_hf_agent(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
    if not openllm.utils.is_transformers_supports_agent(): raise RuntimeError("This version of transformers does not support agent.run. Make sure to upgrade to transformers>4.30.0")
    if len(args) > 1: raise ValueError("'args' should only take one positional argument.")
    task = kwargs.pop("task", args[0])
    return_code = kwargs.pop("return_code", False)
    remote = kwargs.pop("remote", False)

    from transformers.tools.agents import clean_code_for_run, get_tool_creation_code, resolve_tools
    from transformers.tools.python_interpreter import evaluate

    _hf_agent = self._hf_agent

    prompt = t.cast(str, _hf_agent.format_prompt(task))
    stop = ["Task:"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
      response = await client.post(_hf_agent.url_endpoint, json={"inputs": prompt, "parameters": {"max_new_tokens": 200, "return_full_text": False, "stop": stop},},)
      if response.status_code != HTTPStatus.OK:
        raise ValueError(f"Error {response.status_code}: {response.json()}")

    result = response.json()[0]["generated_text"]
    # Inference API returns the stop sequence
    for stop_seq in stop:
      if result.endswith(stop_seq):
        result = result[:-len(stop_seq)]
        break

    # the below have the same logic as agent.run API
    explanation, code = clean_code_for_run(result)
    _hf_agent.log(f"==Explanation from the agent==\n{explanation}")
    _hf_agent.log(f"\n\n==Code generated by the agent==\n{code}")
    if not return_code:
      _hf_agent.log("\n\n==Result==")
      _hf_agent.cached_tools = resolve_tools(code, _hf_agent.toolbox, remote=remote, cached_tools=_hf_agent.cached_tools)
      return evaluate(code, _hf_agent.cached_tools, state=kwargs.copy())
    else:
      tool_code = get_tool_creation_code(code, _hf_agent.toolbox, remote=remote)
      return f"{tool_code}\n{code}"
