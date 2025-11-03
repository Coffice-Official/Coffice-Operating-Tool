"""
Logging utilities for GitHub Image Uploader.

This module provides structured logging with different levels, configurable
output destinations, and progress tracking for bulk operations.
"""

import logging
import sys
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path


class ProgressTracker:
    """Tracks progress for bulk operations."""
    
    def __init__(self, total: int, operation_name: str = "Processing"):
        self.total = total
        self.current = 0
        self.operation_name = operation_name
        self.start_time = datetime.now()
    
    def update(self, increment: int = 1) -> None:
        """Update progress counter."""
        self.current += increment
    
    def get_progress_message(self) -> str:
        """Get formatted progress message."""
        percentage = (self.current / self.total) * 100 if self.total > 0 else 0
        elapsed = datetime.now() - self.start_time
        return f"{self.operation_name}: {self.current}/{self.total} ({percentage:.1f}%) - Elapsed: {elapsed}"


class ComponentLogger:
    """Logger for specific components with structured formatting."""
    
    def __init__(self, component_name: str, logger: logging.Logger):
        self.component_name = component_name
        self.logger = logger
    
    def _format_message(self, message: str) -> str:
        """Format message with component name."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] [%(levelname)s] [{self.component_name}] {message}"
    
    def debug(self, message: str, **kwargs) -> None:
        """Log debug message."""
        formatted_msg = self._format_message(message)
        self.logger.debug(formatted_msg, **kwargs)
    
    def info(self, message: str, **kwargs) -> None:
        """Log info message."""
        formatted_msg = self._format_message(message)
        self.logger.info(formatted_msg, **kwargs)
    
    def warning(self, message: str, **kwargs) -> None:
        """Log warning message."""
        formatted_msg = self._format_message(message)
        self.logger.warning(formatted_msg, **kwargs)
    
    def error(self, message: str, **kwargs) -> None:
        """Log error message."""
        formatted_msg = self._format_message(message)
        self.logger.error(formatted_msg, **kwargs)
    
    def progress(self, tracker: ProgressTracker) -> None:
        """Log progress update."""
        self.info(tracker.get_progress_message())


class LoggerManager:
    """Manages application logging configuration and component loggers."""
    
    def __init__(self, 
                 log_level: str = "INFO",
                 log_file: Optional[str] = None,
                 console_output: bool = True):
        """
        Initialize logger manager.
        
        Args:
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            log_file: Optional log file path
            console_output: Whether to output to console
        """
        self.log_level = getattr(logging, log_level.upper())
        self.log_file = log_file
        self.console_output = console_output
        self.logger = self._setup_logger()
        self.component_loggers: Dict[str, ComponentLogger] = {}
    
    def _setup_logger(self) -> logging.Logger:
        """Set up the main logger with handlers and formatting."""
        logger = logging.getLogger("github_image_uploader")
        logger.setLevel(self.log_level)
        
        # Clear existing handlers
        logger.handlers.clear()
        
        # Create formatter
        formatter = logging.Formatter('%(message)s')
        
        # Console handler
        if self.console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(self.log_level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # File handler
        if self.log_file:
            # Ensure log directory exists
            log_path = Path(self.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_handler = logging.FileHandler(self.log_file)
            file_handler.setLevel(self.log_level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        
        return logger
    
    def get_component_logger(self, component_name: str) -> ComponentLogger:
        """
        Get or create a component logger.
        
        Args:
            component_name: Name of the component (e.g., 'DATABASE', 'GITHUB_API')
            
        Returns:
            ComponentLogger instance for the component
        """
        if component_name not in self.component_loggers:
            self.component_loggers[component_name] = ComponentLogger(
                component_name, self.logger
            )
        return self.component_loggers[component_name]
    
    def create_progress_tracker(self, total: int, operation_name: str = "Processing") -> ProgressTracker:
        """
        Create a progress tracker for bulk operations.
        
        Args:
            total: Total number of items to process
            operation_name: Name of the operation being tracked
            
        Returns:
            ProgressTracker instance
        """
        return ProgressTracker(total, operation_name)
    
    def log_upload_success(self, image_name: str, github_path: str) -> None:
        """Log successful image upload."""
        logger = self.get_component_logger("UPLOADER")
        logger.info(f"Successfully uploaded image: {image_name} -> {github_path}")
    
    def log_upload_failure(self, image_name: str, error_details: str) -> None:
        """Log failed image upload."""
        logger = self.get_component_logger("UPLOADER")
        logger.error(f"Failed to upload image: {image_name} - Error: {error_details}")
    
    def log_database_connection_error(self, error_details: str) -> None:
        """Log database connection failure."""
        logger = self.get_component_logger("DATABASE")
        logger.error(f"Database connection failed: {error_details}")
    
    def log_rate_limit_handling(self, reset_time: int) -> None:
        """Log GitHub API rate limit handling."""
        logger = self.get_component_logger("GITHUB_API")
        logger.warning(f"Rate limit reached. Waiting until reset time: {reset_time}")


# Global logger manager instance
_logger_manager: Optional[LoggerManager] = None


def initialize_logging(log_level: str = "INFO", 
                      log_file: Optional[str] = None,
                      console_output: bool = True) -> LoggerManager:
    """
    Initialize the global logging system.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional log file path
        console_output: Whether to output to console
        
    Returns:
        LoggerManager instance
    """
    global _logger_manager
    _logger_manager = LoggerManager(log_level, log_file, console_output)
    return _logger_manager


def get_logger_manager() -> LoggerManager:
    """
    Get the global logger manager instance.
    
    Returns:
        LoggerManager instance
        
    Raises:
        RuntimeError: If logging has not been initialized
    """
    if _logger_manager is None:
        raise RuntimeError("Logging not initialized. Call initialize_logging() first.")
    return _logger_manager


def get_component_logger(component_name: str) -> ComponentLogger:
    """
    Get a component logger.
    
    Args:
        component_name: Name of the component
        
    Returns:
        ComponentLogger instance
    """
    return get_logger_manager().get_component_logger(component_name)