import logging
import os
from typing import Any, Optional, Union

import aiohttp
import backoff
import requests
import tiktoken

from fuzzyai.enums import EnvironmentVariables, LLMRole
from fuzzyai.handlers.tokenizers.handler import TokensHandler  # type: ignore
from fuzzyai.llm.models import BaseLLMProviderResponse
from fuzzyai.llm.providers.base import (BaseLLMMessage, BaseLLMProvider, BaseLLMProviderException,
                                        BaseLLMProviderRateLimitException, llm_provider_fm)
from fuzzyai.llm.providers.enums import LLMProvider, LLMProviderExtraParams
from fuzzyai.llm.providers.openai.models import OpenAIChatRequest
from fuzzyai.llm.providers.shared.decorators import api_endpoint, sync_api_endpoint

logger = logging.getLogger(__name__)

# Try to import transformers for Qwen tokenizer fallback
try:
    from transformers import AutoTokenizer
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    AutoTokenizer = None  # type: ignore

class OpenAIProviderException(BaseLLMProviderException):
    pass

class OpenAIConfig:
    API_BASE_URL = "https://api.openai.com/v1"
    CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"
    API_KEY_ENV_VAR = EnvironmentVariables.OPENAI_API_KEY.value
    BASE_URL_ENV_VAR = EnvironmentVariables.OPENAI_BASE_URL.value
    O1_FAMILY_MODELS = {"o1-mini", "o1-preview", "o3-mini"}


@llm_provider_fm.flavor(LLMProvider.OPENAI)
class OpenAIProvider(BaseLLMProvider):
    def __init__(self, model: str, **extra: Any):
        super().__init__(model=model, **extra)

        api_key = extra.get("api_key") or os.environ.get(OpenAIConfig.API_KEY_ENV_VAR)
        if api_key is None:
            raise BaseLLMProviderException(
                f"{OpenAIConfig.API_KEY_ENV_VAR} not found in extra parameters or os.environ"
            )

        base_url = (
            extra.get("base_url")
            or os.environ.get(OpenAIConfig.BASE_URL_ENV_VAR)
            or OpenAIConfig.API_BASE_URL
        )
        
        if base_url and base_url.endswith('/'):
            base_url = base_url.rstrip('/')

        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        self._session = aiohttp.ClientSession(headers=self._headers)
        self._base_url = base_url
        logger.info(f"OpenAI provider initialized with base_url: {self._base_url} (model: {model})")

        # Initialize tokenizer
        self._tokenizer: Optional[Union[tiktoken.Encoding, Any]] = None
        self.tokens_handler = None
        
        try:
            # Try tiktoken first (for OpenAI models)
            self._tokenizer = tiktoken.encoding_for_model(model_name=model)
            self.tokens_handler = TokensHandler(tokenizer=self._tokenizer)
            logger.debug(f"Tokenizer initialized using tiktoken for model {model}")
        except Exception as tiktoken_ex:
            # Fallback to transformers for non-OpenAI models (like Qwen)
            if model.startswith("qwen") or "qwen" in model.lower():
                if TRANSFORMERS_AVAILABLE:
                    try:
                        # Try different Qwen tokenizer models in order of preference
                        tokenizer_model_names = []
                        
                        if "qwen2.5" in model.lower() and "vl" in model.lower():
                            # For Qwen2.5-VL models, try VL-specific tokenizer first
                            tokenizer_model_names = [
                                "Qwen/Qwen2-VL-7B-Instruct",
                                "Qwen/Qwen2-VL-2B-Instruct",
                                "Qwen/Qwen2.5-7B-Instruct",
                                "Qwen/Qwen2.5-3B-Instruct",
                            ]
                        elif "qwen2.5" in model.lower():
                            # For Qwen2.5 models
                            tokenizer_model_names = [
                                "Qwen/Qwen2.5-7B-Instruct",
                                "Qwen/Qwen2.5-3B-Instruct",
                                "Qwen/Qwen2.5-1.5B-Instruct",
                            ]
                        else:
                            # For other Qwen models
                            tokenizer_model_names = [
                                "Qwen/Qwen2-7B-Instruct",
                                "Qwen/Qwen2-1.5B-Instruct",
                            ]
                        
                        qwen_tokenizer = None
                        last_exception = None
                        
                        for tokenizer_model_name in tokenizer_model_names:
                            try:
                                logger.info(f"Attempting to load Qwen tokenizer from {tokenizer_model_name} for model {model}")
                                qwen_tokenizer = AutoTokenizer.from_pretrained(
                                    tokenizer_model_name,
                                    trust_remote_code=True,
                                    use_fast=True
                                )
                                logger.info(f"Successfully loaded Qwen tokenizer from {tokenizer_model_name}")
                                break
                            except Exception as tokenizer_ex:
                                last_exception = tokenizer_ex
                                logger.debug(f"Failed to load tokenizer from {tokenizer_model_name}: {tokenizer_ex}")
                                continue
                        
                        if qwen_tokenizer:
                            self._tokenizer = qwen_tokenizer
                            self.tokens_handler = TokensHandler(tokenizer=self._tokenizer)
                            logger.info(f"Tokenizer initialized using transformers for Qwen model {model}")
                        else:
                            raise last_exception if last_exception else Exception("No suitable Qwen tokenizer found")
                            
                    except Exception as transformers_ex:
                        logger.warning(f"Failed to initialize Qwen tokenizer: {transformers_ex}")
                        logger.warning(f"Tokenizer not initialized for model {model}, some attacks might not function properly")
                        self.tokens_handler = None
                        self._tokenizer = None
                else:
                    logger.warning(f"Transformers not available, cannot initialize tokenizer for Qwen model {model}")
                    logger.warning(f"Tokenizer not initialized for model {model}, some attacks might not function properly")
            else:
                logger.warning(f"Tokenizer not initialized for model {model} (tiktoken error: {tiktoken_ex}), some attacks might not function properly")

    @classmethod
    def get_supported_models(cls) -> Union[list[str], str]:
        return ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4.1-nano", "o1-mini", "o1-preview", "o3-mini", "gpt-4.5-preview", "qwen2.5-vl-72b-instruct"]

    
    @api_endpoint(OpenAIConfig.CHAT_COMPLETIONS_ENDPOINT)
    async def generate(self, prompt: str, url: str, system_prompt: Optional[str] = None, **extra: Any) -> Optional[BaseLLMProviderResponse]:
        messages = [BaseLLMMessage(role=LLMRole.USER, content=prompt)]
        messages = self._prepare_messages(messages, system_prompt)
        return await self.chat(messages=messages, **extra) # type: ignore
    
    @backoff.on_exception(backoff.expo, BaseLLMProviderRateLimitException, max_value=10)
    @api_endpoint(OpenAIConfig.CHAT_COMPLETIONS_ENDPOINT)
    async def chat(self, messages: list[BaseLLMMessage], url: str, system_prompt: Optional[str] = None, **extra: Any) -> BaseLLMProviderResponse:
        messages = self._prepare_messages(messages, system_prompt)
        try:
            logger.debug(f"Making request to URL: {url}")
            request = OpenAIChatRequest(model=self._model_name, messages=messages, **extra)
            async with self._session.post(url, json=request.model_dump()) as response:
                openai_response = await response.json()

                self._handle_error_response(openai_response)
                choice = openai_response["choices"][0]
                if choice.get('finish_reason') == 'length':
                    logger.warning('OpenAI response was truncated! Please increase the token limit by setting -N=<max tokens>')

                content = choice['message'].get('content')
                if content is None:
                    logger.warning('OpenAI response content is None, using empty string as fallback')
                    content = ''
                
                return BaseLLMProviderResponse(response=content)
        except (BaseLLMProviderRateLimitException, OpenAIProviderException) as e:
            raise e
        except Exception as e:            
            logger.error(f'Error generating text: {e}')
            raise OpenAIProviderException('Cant generate text')
    
    @backoff.on_exception(backoff.expo, BaseLLMProviderRateLimitException, max_value=10)
    def sync_generate(self, prompt: str, **extra: Any) -> Optional[BaseLLMProviderResponse]:
        messages = [BaseLLMMessage(role=LLMRole.USER, content=prompt)]
        
        if extra.get(LLMProviderExtraParams.APPEND_LAST_RESPONSE) and (history := self.get_history()):
            messages.append(BaseLLMMessage(role=LLMRole.ASSISTANT, content=history[-1].response))
        
        chat_extra_params = {k:v for k, v in extra.items() if k not in [LLMProviderExtraParams.APPEND_LAST_RESPONSE]}
        return self.sync_chat(messages, **chat_extra_params)  # type: ignore

    @sync_api_endpoint(OpenAIConfig.CHAT_COMPLETIONS_ENDPOINT)
    def sync_chat(self, messages: list[BaseLLMMessage], url: str, 
                  system_prompt: Optional[str] = None, **extra: Any) -> Optional[BaseLLMProviderResponse]:
        messages = self._prepare_messages(messages, system_prompt)

        try:
            request = OpenAIChatRequest(model=self._model_name, messages=messages, **extra)
            with requests.post(url, json=request.model_dump(), headers=self._headers) as response:
                openai_response = response.json()
                self._handle_error_response(openai_response)
                
                choice = openai_response["choices"][0]
                content = choice['message'].get('content')
                if content is None:
                    logger.warning('OpenAI response content is None, using empty string as fallback')
                    content = ''
                    
                return BaseLLMProviderResponse(response=content)
        except (BaseLLMProviderRateLimitException, OpenAIProviderException) as e:
            raise e
        except Exception as e:            
            logger.error(f'Error generating text: {e}')
            raise OpenAIProviderException('Cant generate text')
    
    async def close(self) -> None:
        await self._session.close()

    def _prepare_messages(self, messages: list[BaseLLMMessage], 
                          system_prompt: Optional[str] = None) -> list[BaseLLMMessage]:
        if system_prompt and self._model_name not in OpenAIConfig.O1_FAMILY_MODELS:
            return [BaseLLMMessage(role=LLMRole.SYSTEM, content=system_prompt)] + messages
        return messages
    
    @staticmethod
    def _handle_error_response(response_data: dict[str, Any]) -> None:
        if error := response_data.get("error"):
            if error.get("code") == "rate_limit_exceeded":
                raise BaseLLMProviderRateLimitException("Rate limit exceeded")
            raise OpenAIProviderException(f"OpenAI error: {error.get('message', 'Unknown error')}")

