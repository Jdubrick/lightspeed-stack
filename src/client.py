"""LLama stack client retrieval."""

import logging

from llama_stack.distribution.library_client import LlamaStackAsLibraryClient
from llama_stack_client import LlamaStackClient
from models.config import LLamaStackConfiguration

logger = logging.getLogger(__name__)


def get_llama_stack_client(
    llama_stack_config: LLamaStackConfiguration,
) -> LlamaStackClient:
    if llama_stack_config.use_as_library_client is True:
        logger.info("Using Llama stack as library client")
        client = LlamaStackAsLibraryClient(
            llama_stack_config.library_client_config_path
        )
        client.initialize()
        return client
    else:
        logger.info("Using Llama stack running as a service")
        return LlamaStackClient(
            base_url=llama_stack_config.url, api_key=llama_stack_config.api_key
        )
