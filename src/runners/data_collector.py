"""Data collector runner."""

import logging

from configuration import configuration
from services.data_collector import DataCollectorService

logger: logging.Logger = logging.getLogger(__name__)


def start_data_collector() -> None:
    """Start the data collector service as a standalone process."""
    logger.info("Starting data collector runner")
    
    collector_config = configuration.user_data_collection_configuration.data_collector
    
    if not collector_config.enabled:
        logger.info("Data collection is disabled")
        return
    
    service = DataCollectorService()
    service.run() 