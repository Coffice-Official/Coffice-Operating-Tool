"""
GitHub API client module for uploading images to GitHub repository.

This module provides a GitHubClient class that handles authentication,
image uploads using the GitHub Contents API, and implements rate limiting
and retry logic for robust operation.
"""

import base64
import time
import logging
from typing import Dict, Any, Optional, Tuple, List
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config


class GitHubAPIError(Exception):
    """Raised when GitHub API operations fail."""
    pass


class RateLimitError(GitHubAPIError):
    """Raised when GitHub API rate limit is exceeded."""
    pass


class AuthenticationError(GitHubAPIError):
    """Raised when GitHub API authentication fails."""
    pass


class GitHubClient:
    """
    GitHub API client for uploading images to repository.
    
    This client handles authentication, base64 encoding of image data,
    and implements retry logic with exponential backoff for network operations.
    """
    
    def __init__(self, config: Config):
        """
        Initialize GitHub client with configuration.
        
        Args:
            config: Configuration object containing GitHub settings
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # GitHub API base URL
        self.api_base_url = "https://api.github.com"
        
        # Initialize requests session with retry strategy
        self.session = self._create_session()
        
        # Rate limiting tracking
        self.rate_limit_remaining = None
        self.rate_limit_reset = None
        
        # Validate authentication on initialization
        self._validate_authentication()
    
    def _create_session(self) -> requests.Session:
        """
        Create a requests session with retry strategy and authentication.
        
        Returns:
            Configured requests session
        """
        session = requests.Session()
        
        # Set authentication header
        session.headers.update({
            'Authorization': f'token {self.config.github_token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'GitHub-Image-Uploader/1.0'
        })
        
        # Configure retry strategy for network errors
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "PUT", "POST"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session
    
    def _validate_authentication(self) -> None:
        """
        Validate GitHub authentication by making a test API call.
        
        Raises:
            AuthenticationError: If authentication fails
            GitHubAPIError: If API call fails for other reasons
        """
        try:
            url = f"{self.api_base_url}/user"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 401:
                raise AuthenticationError(
                    "GitHub authentication failed. Please check your GITHUB_TOKEN."
                )
            elif response.status_code == 403:
                # Check if it's a rate limit issue
                if 'X-RateLimit-Remaining' in response.headers:
                    remaining = int(response.headers['X-RateLimit-Remaining'])
                    if remaining == 0:
                        reset_time = int(response.headers['X-RateLimit-Reset'])
                        raise RateLimitError(
                            f"GitHub API rate limit exceeded. Resets at {reset_time}"
                        )
                raise AuthenticationError(
                    "GitHub API access forbidden. Check token permissions."
                )
            elif not response.ok:
                raise GitHubAPIError(
                    f"GitHub API validation failed: {response.status_code} - {response.text}"
                )
            
            # Update rate limit info
            self._update_rate_limit_info(response)
            
            user_info = response.json()
            self.logger.info(f"GitHub authentication successful for user: {user_info.get('login', 'unknown')}")
            
        except requests.exceptions.RequestException as e:
            raise GitHubAPIError(f"Network error during GitHub authentication: {str(e)}")
    
    def _update_rate_limit_info(self, response: requests.Response) -> None:
        """
        Update rate limit information from response headers.
        
        Args:
            response: HTTP response from GitHub API
        """
        if 'X-RateLimit-Remaining' in response.headers:
            self.rate_limit_remaining = int(response.headers['X-RateLimit-Remaining'])
        
        if 'X-RateLimit-Reset' in response.headers:
            self.rate_limit_reset = int(response.headers['X-RateLimit-Reset'])
    
    def _handle_rate_limit(self, force_check: bool = False) -> None:
        """
        Handle GitHub API rate limiting by waiting for reset time.
        
        Args:
            force_check: If True, check rate limit even if remaining > 1
        
        Raises:
            RateLimitError: If rate limit is exceeded and reset time is too far
        """
        # Check if we need to handle rate limiting
        should_wait = False
        wait_time = 0
        
        if self.rate_limit_remaining is not None:
            # Be more conservative with rate limiting
            if self.rate_limit_remaining <= 5 or force_check:
                if self.rate_limit_reset:
                    current_time = int(time.time())
                    wait_time = self.rate_limit_reset - current_time
                    
                    if wait_time > 0:
                        should_wait = True
                        
                        # Don't wait more than 1 hour
                        if wait_time > 3600:
                            raise RateLimitError(
                                f"Rate limit exceeded. Reset time is too far: {wait_time} seconds. "
                                f"Remaining requests: {self.rate_limit_remaining}"
                            )
                        
                        # Add progressive delay based on remaining requests
                        if self.rate_limit_remaining <= 1:
                            # Critical: wait full time plus buffer
                            wait_time += 5
                        elif self.rate_limit_remaining <= 3:
                            # Warning: wait full time
                            pass
                        else:
                            # Caution: wait partial time
                            wait_time = min(wait_time, 60)  # Max 1 minute for caution
        
        if should_wait:
            self.logger.warning(
                f"Rate limit handling: {self.rate_limit_remaining} requests remaining. "
                f"Waiting {wait_time} seconds until reset..."
            )
            
            # Sleep in chunks to allow for interruption
            chunk_size = 10  # 10 second chunks
            remaining_wait = wait_time
            
            while remaining_wait > 0:
                sleep_time = min(chunk_size, remaining_wait)
                time.sleep(sleep_time)
                remaining_wait -= sleep_time
                
                if remaining_wait > 0:
                    self.logger.info(f"Rate limit wait: {remaining_wait} seconds remaining...")
            
            self.logger.info("Rate limit wait completed, resuming operations")
    
    def _encode_content(self, binary_data: bytes) -> str:
        """
        Encode binary data to base64 string for GitHub API.
        
        Args:
            binary_data: Binary image data
            
        Returns:
            Base64 encoded string
        """
        return base64.b64encode(binary_data).decode('utf-8')
    
    def _generate_commit_message(self, filename: str) -> str:
        """
        Generate commit message for image upload.
        
        Args:
            filename: Name of the file being uploaded
            
        Returns:
            Formatted commit message
        """
        return self.config.commit_message_template.format(filename=filename)
    
    def upload_file(self, filename: str, file_data: bytes, 
                    custom_path: Optional[str] = None, 
                    is_binary: bool = True) -> Dict[str, Any]:
        """
        Upload file (image or CSV) to GitHub repository using Contents API.
        
        Args:
            filename: Name of the file to upload
            file_data: Binary file data
            custom_path: Optional custom path within upload directory
            is_binary: Whether the file is binary (True) or text (False)
            
        Returns:
            Dictionary containing upload response data with keys:
            - 'path': Full path in repository
            - 'download_url': Direct download URL
            - 'sha': File SHA
            - 'commit_sha': Commit SHA
            
        Raises:
            GitHubAPIError: If upload fails
            RateLimitError: If rate limit is exceeded
            AuthenticationError: If authentication fails
        """
        # Check rate limit before making request
        self._handle_rate_limit()
        
        # Construct file path
        if custom_path:
            file_path = f"{self.config.upload_path}/{custom_path}/{filename}"
        else:
            file_path = f"{self.config.upload_path}/{filename}"
        
        # Encode image data
        encoded_content = self._encode_content(image_data)
        
        # Prepare API request
        url = (f"{self.api_base_url}/repos/{self.config.github_owner}/"
               f"{self.config.github_repo}/contents/{file_path}")
        
        payload = {
            'message': self._generate_commit_message(filename),
            'content': encoded_content,
            'branch': self.config.branch
        }
        
        # Check if file already exists
        existing_file = self._get_file_info(file_path)
        if existing_file:
            payload['sha'] = existing_file['sha']
            self.logger.info(f"File {file_path} already exists, updating...")
        
        # Make upload request with enhanced retry logic
        max_retries = 5  # Increased from 3 for better resilience
        base_delay = 1.0  # Base delay for exponential backoff
        
        for attempt in range(max_retries):
            try:
                # Log attempt details
                self.logger.info(f"Upload attempt {attempt + 1}/{max_retries} for {filename}")
                
                response = self.session.put(url, json=payload, timeout=120)  # Increased timeout
                
                # Update rate limit info from response
                self._update_rate_limit_info(response)
                
                # Handle different response codes
                if response.status_code == 401:
                    raise AuthenticationError(
                        f"GitHub authentication failed during upload of {filename}. "
                        f"Please check your GITHUB_TOKEN permissions."
                    )
                elif response.status_code == 403:
                    # Check if it's a rate limit issue
                    if 'X-RateLimit-Remaining' in response.headers:
                        remaining = int(response.headers['X-RateLimit-Remaining'])
                        if remaining == 0:
                            reset_time = int(response.headers['X-RateLimit-Reset'])
                            self.logger.warning(f"Rate limit hit during upload, handling gracefully...")
                            self._handle_rate_limit(force_check=True)
                            # After waiting, retry this attempt
                            continue
                    
                    # Check for other 403 reasons
                    error_text = response.text
                    if 'rate limit' in error_text.lower():
                        self.logger.warning("Rate limit detected in error message, handling...")
                        self._handle_rate_limit(force_check=True)
                        continue
                    elif 'permission' in error_text.lower() or 'forbidden' in error_text.lower():
                        raise AuthenticationError(
                            f"Insufficient permissions to upload {filename}. "
                            f"Check repository access and token permissions. Error: {error_text}"
                        )
                    else:
                        raise GitHubAPIError(f"Upload forbidden for {filename}: {error_text}")
                        
                elif response.status_code == 422:
                    # Unprocessable entity - handle various scenarios
                    try:
                        error_data = response.json()
                        error_message = error_data.get('message', str(error_data))
                    except:
                        error_message = response.text
                    
                    if 'already exists' in error_message.lower() or 'sha' in error_message.lower():
                        # File exists but we don't have the SHA, try to get it
                        self.logger.info(f"File {filename} already exists, attempting to get SHA for update...")
                        existing_file = self._get_file_info(file_path)
                        
                        if existing_file and 'sha' in existing_file and attempt < max_retries - 1:
                            payload['sha'] = existing_file['sha']
                            self.logger.info(f"Retrieved existing SHA for {filename}, retrying update...")
                            continue
                        else:
                            raise GitHubAPIError(
                                f"File {filename} already exists but could not retrieve SHA for update. "
                                f"Error: {error_message}"
                            )
                    else:
                        raise GitHubAPIError(f"Upload validation failed for {filename}: {error_message}")
                        
                elif response.status_code == 409:
                    # Conflict - usually means file was modified since we checked
                    self.logger.warning(f"Conflict detected for {filename}, refreshing file info...")
                    existing_file = self._get_file_info(file_path)
                    if existing_file and attempt < max_retries - 1:
                        payload['sha'] = existing_file['sha']
                        continue
                    else:
                        raise GitHubAPIError(f"Conflict uploading {filename}: {response.text}")
                        
                elif response.status_code >= 500:
                    # Server errors - definitely retry
                    if attempt < max_retries - 1:
                        wait_time = base_delay * (2 ** attempt) + (attempt * 0.5)  # Jittered exponential backoff
                        self.logger.warning(
                            f"Server error {response.status_code} for {filename} (attempt {attempt + 1}). "
                            f"Retrying in {wait_time:.1f} seconds..."
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        raise GitHubAPIError(
                            f"Server error persisted for {filename} after {max_retries} attempts: "
                            f"{response.status_code} - {response.text}"
                        )
                        
                elif not response.ok:
                    # Other client errors
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    
                    # Some 4xx errors might be retryable (like 429 without proper headers)
                    if response.status_code == 429 or (400 <= response.status_code < 500 and attempt < max_retries - 1):
                        wait_time = base_delay * (2 ** attempt)
                        self.logger.warning(
                            f"Client error {response.status_code} for {filename} (attempt {attempt + 1}). "
                            f"Retrying in {wait_time:.1f} seconds... Error: {error_msg}"
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        raise GitHubAPIError(f"Upload failed for {filename}: {error_msg}")
                
                # Success - parse response
                try:
                    response_data = response.json()
                except ValueError as e:
                    raise GitHubAPIError(f"Invalid JSON response for {filename}: {str(e)}")
                
                # Validate response structure
                if 'content' not in response_data:
                    raise GitHubAPIError(f"Invalid response structure for {filename}: missing 'content'")
                
                content = response_data['content']
                required_fields = ['path', 'download_url', 'sha']
                missing_fields = [field for field in required_fields if field not in content]
                
                if missing_fields:
                    raise GitHubAPIError(
                        f"Invalid response for {filename}: missing fields {missing_fields}"
                    )
                
                result = {
                    'path': content['path'],
                    'download_url': content['download_url'],
                    'sha': content['sha'],
                    'commit_sha': response_data.get('commit', {}).get('sha', 'unknown')
                }
                
                self.logger.info(f"Successfully uploaded {filename} to {result['path']}")
                return result
    
    def upload_image(self, filename: str, image_data: bytes, 
                    custom_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Upload image to GitHub repository using Contents API.
        
        Args:
            filename: Name of the file to upload
            image_data: Binary image data
            custom_path: Optional custom path within upload directory
            
        Returns:
            Dictionary containing upload response data
            
        Raises:
            GitHubAPIError: If upload fails
        """
        return self.upload_file(filename, image_data, custom_path, is_binary=True)
                
            except (AuthenticationError, RateLimitError):
                # Don't retry authentication or rate limit errors that we've already handled
                raise
                
            except requests.exceptions.Timeout as e:
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Timeout uploading {filename} (attempt {attempt + 1}): {str(e)}. "
                        f"Retrying in {wait_time:.1f} seconds..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    raise GitHubAPIError(f"Timeout uploading {filename} after {max_retries} attempts: {str(e)}")
                    
            except requests.exceptions.ConnectionError as e:
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Connection error uploading {filename} (attempt {attempt + 1}): {str(e)}. "
                        f"Retrying in {wait_time:.1f} seconds..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    raise GitHubAPIError(f"Connection error uploading {filename} after {max_retries} attempts: {str(e)}")
                    
            except requests.exceptions.RequestException as e:
                # Other request exceptions
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Network error uploading {filename} (attempt {attempt + 1}): {str(e)}. "
                        f"Retrying in {wait_time:.1f} seconds..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    raise GitHubAPIError(f"Network error uploading {filename} after {max_retries} attempts: {str(e)}")
            
            except Exception as e:
                # Unexpected errors
                self.logger.error(f"Unexpected error uploading {filename} (attempt {attempt + 1}): {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    self.logger.warning(f"Retrying in {wait_time:.1f} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise GitHubAPIError(f"Unexpected error uploading {filename} after {max_retries} attempts: {str(e)}")
        
        # This should never be reached due to the loop logic above
        raise GitHubAPIError(f"Upload failed for {filename} after all retry attempts")
    
    def _get_file_info(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Get information about an existing file in the repository.
        
        Args:
            file_path: Path to the file in repository
            
        Returns:
            File information dictionary or None if file doesn't exist
        """
        try:
            url = (f"{self.api_base_url}/repos/{self.config.github_owner}/"
                   f"{self.config.github_repo}/contents/{file_path}")
            
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 404:
                return None
            elif response.ok:
                return response.json()
            else:
                self.logger.warning(
                    f"Failed to get file info for {file_path}: "
                    f"{response.status_code} - {response.text}"
                )
                return None
                
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Network error getting file info for {file_path}: {str(e)}")
            return None
    
    def batch_upload_images(self, images: List[Tuple[str, bytes, Optional[str]]], 
                           max_concurrent: int = 3) -> Dict[str, Any]:
        """
        Upload multiple images with rate limiting and error handling.
        
        Args:
            images: List of tuples (filename, image_data, custom_path)
            max_concurrent: Maximum number of concurrent uploads (respects rate limits)
            
        Returns:
            Dictionary with batch upload results:
            - 'successful': List of successful upload results
            - 'failed': List of failed uploads with errors
            - 'total': Total number of images processed
        """
        results = {
            'successful': [],
            'failed': [],
            'total': len(images)
        }
        
        if not images:
            return results
        
        self.logger.info(f"Starting batch upload of {len(images)} images")
        
        for i, (filename, image_data, custom_path) in enumerate(images):
            try:
                # Check rate limit before each upload
                if self.rate_limit_remaining is not None and self.rate_limit_remaining <= 2:
                    self.logger.info(f"Rate limit low ({self.rate_limit_remaining}), handling before upload {i+1}")
                    self._handle_rate_limit(force_check=True)
                
                # Upload single image
                result = self.upload_image(filename, image_data, custom_path)
                results['successful'].append({
                    'filename': filename,
                    'result': result
                })
                
                self.logger.info(f"Batch upload progress: {i+1}/{len(images)} completed")
                
                # Small delay between uploads to be respectful to the API
                if i < len(images) - 1:  # Don't sleep after the last upload
                    time.sleep(0.5)
                    
            except (AuthenticationError, RateLimitError) as e:
                # Critical errors that should stop the batch
                self.logger.error(f"Critical error in batch upload at image {i+1}: {str(e)}")
                results['failed'].append({
                    'filename': filename,
                    'error': str(e),
                    'error_type': type(e).__name__
                })
                # Continue with remaining images after logging the error
                
            except GitHubAPIError as e:
                # API errors for individual images
                self.logger.warning(f"Failed to upload {filename} in batch: {str(e)}")
                results['failed'].append({
                    'filename': filename,
                    'error': str(e),
                    'error_type': 'GitHubAPIError'
                })
                
            except Exception as e:
                # Unexpected errors
                self.logger.error(f"Unexpected error uploading {filename} in batch: {str(e)}")
                results['failed'].append({
                    'filename': filename,
                    'error': str(e),
                    'error_type': 'UnexpectedError'
                })
        
        # Log batch results
        success_count = len(results['successful'])
        failed_count = len(results['failed'])
        success_rate = (success_count / len(images)) * 100 if images else 0
        
        self.logger.info(f"Batch upload completed: {success_count} successful, {failed_count} failed ({success_rate:.1f}% success rate)")
        
        return results
    
    def get_rate_limit_status(self) -> Dict[str, Any]:
        """
        Get current rate limit status with enhanced error handling.
        
        Returns:
            Dictionary with rate limit information
        """
        try:
            url = f"{self.api_base_url}/rate_limit"
            response = self.session.get(url, timeout=30)
            
            if response.ok:
                data = response.json()
                # Update our internal tracking
                if 'rate' in data and 'remaining' in data['rate']:
                    self.rate_limit_remaining = data['rate']['remaining']
                if 'rate' in data and 'reset' in data['rate']:
                    self.rate_limit_reset = data['rate']['reset']
                return data
            else:
                self.logger.warning(f"Failed to get rate limit status: {response.status_code}")
                return {
                    'rate': {
                        'remaining': self.rate_limit_remaining,
                        'reset': self.rate_limit_reset,
                        'limit': 5000  # Default GitHub limit
                    },
                    'error': f"HTTP {response.status_code}"
                }
                
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Network error getting rate limit status: {str(e)}")
            return {
                'rate': {
                    'remaining': self.rate_limit_remaining,
                    'reset': self.rate_limit_reset,
                    'limit': 5000
                },
                'error': str(e)
            }
    
    def close(self) -> None:
        """Close the HTTP session."""
        if self.session:
            self.session.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()