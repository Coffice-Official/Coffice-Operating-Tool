"""
Configuration management module for GitHub Image Uploader.

This module handles loading and validating environment variables
required for the application to function properly.
"""

import os
from typing import Optional
from dotenv import load_dotenv


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing."""
    pass


class Config:
    """Configuration class to load and validate environment variables."""
    
    def __init__(self, env_file: Optional[str] = None):
        """
        Initialize configuration by loading environment variables.
        
        Args:
            env_file: Optional path to .env file to load
        """
        # Load environment variables from .env file if it exists
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()
        
        # Load and validate all configuration
        self._load_github_config()
        self._load_mysql_config()
        self._load_application_config()
    
    def _load_github_config(self) -> None:
        """Load and validate GitHub-related configuration."""
        self.github_token = self._get_required_env('GITHUB_TOKEN')
        self.github_owner = self._get_required_env('GITHUB_OWNER', 'Coffice-Official')
        self.github_repo = self._get_required_env('GITHUB_REPO', 'Coffice-Operating-Tool')
        
        # Validate GitHub token format (should start with 'ghp_' for personal access tokens)
        if not self.github_token.startswith(('ghp_', 'github_pat_')):
            raise ConfigurationError(
                "GITHUB_TOKEN appears to be invalid. "
                "Personal access tokens should start with 'ghp_' or 'github_pat_'"
            )
    
    def _load_mysql_config(self) -> None:
        """Load and validate MySQL-related configuration."""
        self.mysql_host = self._get_required_env('MYSQL_HOST')
        self.mysql_port = self._get_env_as_int('MYSQL_PORT', 3306)
        self.mysql_user = self._get_required_env('MYSQL_USER')
        self.mysql_password = self._get_required_env('MYSQL_PASSWORD')
        self.mysql_database = self._get_required_env('MYSQL_DATABASE')
        
        # Validate port range
        if not (1 <= self.mysql_port <= 65535):
            raise ConfigurationError(
                f"MYSQL_PORT must be between 1 and 65535, got: {self.mysql_port}"
            )
    
    def _load_application_config(self) -> None:
        """Load and validate application-specific configuration."""
        self.image_table = self._get_required_env('IMAGE_TABLE')
        self.path_column = self._get_required_env('PATH_COLUMN')
        
        # Optional configuration with defaults
        self.upload_path = self._get_optional_env('UPLOAD_PATH', 'cafe-image')
        self.commit_message_template = self._get_optional_env(
            'COMMIT_MESSAGE_TEMPLATE', 
            'Upload image: {filename}'
        )
        self.branch = self._get_optional_env('GITHUB_BRANCH', 'main')
    
    def _get_required_env(self, key: str, default: Optional[str] = None) -> str:
        """
        Get a required environment variable.
        
        Args:
            key: Environment variable name
            default: Default value if provided
            
        Returns:
            Environment variable value
            
        Raises:
            ConfigurationError: If the variable is not set and no default provided
        """
        value = os.getenv(key, default)
        if value is None or value.strip() == '':
            raise ConfigurationError(f"Required environment variable '{key}' is not set")
        return value.strip()
    
    def _get_optional_env(self, key: str, default: str) -> str:
        """
        Get an optional environment variable with a default value.
        
        Args:
            key: Environment variable name
            default: Default value to use if not set
            
        Returns:
            Environment variable value or default
        """
        value = os.getenv(key, default)
        return value.strip() if value else default
    
    def _get_env_as_int(self, key: str, default: int) -> int:
        """
        Get an environment variable as an integer.
        
        Args:
            key: Environment variable name
            default: Default value if not set or invalid
            
        Returns:
            Integer value
            
        Raises:
            ConfigurationError: If the value cannot be converted to int
        """
        value = os.getenv(key)
        if value is None:
            return default
        
        try:
            return int(value.strip())
        except ValueError:
            raise ConfigurationError(
                f"Environment variable '{key}' must be a valid integer, got: '{value}'"
            )
    
    def validate(self) -> None:
        """
        Perform additional validation on the loaded configuration.
        
        Raises:
            ConfigurationError: If configuration is invalid
        """
        # Validate table and column names (basic SQL identifier validation)
        for name, value in [
            ('IMAGE_TABLE', self.image_table),
            ('PATH_COLUMN', self.path_column)
        ]:
            if not self._is_valid_sql_identifier(value):
                raise ConfigurationError(
                    f"{name} contains invalid characters. "
                    f"SQL identifiers should only contain letters, numbers, and underscores."
                )
        
        # Validate upload path (should not start with /)
        if self.upload_path.startswith('/'):
            self.upload_path = self.upload_path.lstrip('/')
    
    def _is_valid_sql_identifier(self, identifier: str) -> bool:
        """
        Check if a string is a valid SQL identifier.
        
        Args:
            identifier: String to validate
            
        Returns:
            True if valid, False otherwise
        """
        if not identifier:
            return False
        
        # Must start with letter or underscore
        if not (identifier[0].isalpha() or identifier[0] == '_'):
            return False
        
        # Rest can be letters, numbers, or underscores
        return all(c.isalnum() or c == '_' for c in identifier[1:])
    
    def get_mysql_connection_params(self) -> dict:
        """
        Get MySQL connection parameters as a dictionary.
        
        Returns:
            Dictionary with MySQL connection parameters
        """
        return {
            'host': self.mysql_host,
            'port': self.mysql_port,
            'user': self.mysql_user,
            'password': self.mysql_password,
            'database': self.mysql_database
        }
    
    def get_github_repo_url(self) -> str:
        """
        Get the GitHub repository URL.
        
        Returns:
            GitHub repository URL
        """
        return f"https://github.com/{self.github_owner}/{self.github_repo}"
    
    def __repr__(self) -> str:
        """String representation of config (without sensitive data)."""
        return (
            f"Config("
            f"github_owner='{self.github_owner}', "
            f"github_repo='{self.github_repo}', "
            f"mysql_host='{self.mysql_host}', "
            f"mysql_port={self.mysql_port}, "
            f"mysql_database='{self.mysql_database}', "
            f"image_table='{self.image_table}', "
            f"upload_path='{self.upload_path}'"
            f")"
        )


def load_config(env_file: Optional[str] = None) -> Config:
    """
    Load and validate configuration.
    
    Args:
        env_file: Optional path to .env file
        
    Returns:
        Validated Config instance
        
    Raises:
        ConfigurationError: If configuration is invalid
    """
    config = Config(env_file)
    config.validate()
    return config