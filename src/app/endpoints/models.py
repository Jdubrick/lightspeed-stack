"""Handler for REST API call to provide info."""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request
from llama_stack_client import Agent, AgentEventLogger, RAGDocument, LlamaStackClient

from models.responses import ModelsResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["models"])


models_responses: dict[int | str, dict[str, Any]] = {
    200: {
        "models": [
            {
                "identifier": "all-MiniLM-L6-v2",
                "metadata": {"embedding_dimension": 384},
                "api_model_type": "embedding",
                "provider_id": "ollama",
                "provider_resource_id": "all-minilm:latest",
                "type": "model",
                "model_type": "embedding",
            },
            {
                "identifier": "llama3.2:3b-instruct-fp16",
                "metadata": {},
                "api_model_type": "llm",
                "provider_id": "ollama",
                "provider_resource_id": "llama3.2:3b-instruct-fp16",
                "type": "model",
                "model_type": "llm",
            },
        ]
    },
}


@router.get("/models", responses=models_responses)
def models_endpoint_handler(request: Request) -> ModelsResponse:
    client = LlamaStackClient(base_url="http://localhost:8321")
    models = client.models.list()
    m = [dict(m) for m in models]
    return ModelsResponse(models=m)
