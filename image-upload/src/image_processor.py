"""
Image processing orchestration module for GitHub Image Uploader.

This module provides the ImageProcessor class that coordinates all operations
including database access, GitHub uploads, and logging for batch and individual
image processing workflows.
"""

import base64
import hashlib
import logging
import time
import requests
import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .config import Config
from .database import DatabaseManager, DatabaseError
from .github_client import GitHubClient, GitHubAPIError, RateLimitError, AuthenticationError
from .logger import get_component_logger, get_logger_manager, ProgressTracker


class ImageProcessingError(Exception):
    """Raised when image processing operations fail."""
    pass


class ImageProcessor:
    """
    Main orchestrator for image processing operations.
    
    This class coordinates database operations, GitHub uploads, and logging
    to provide batch and individual image processing capabilities.
    """
    
    def __init__(self, config: Config):
        """
        Initialize ImageProcessor with configuration.
        
        Args:
            config: Configuration instance with all required settings
        """
        self.config = config
        self.logger = get_component_logger("IMAGE_PROCESSOR")
        self._shutdown_requested = False
        
        # Initialize components
        self.db_manager = DatabaseManager(config)
        self.github_client = GitHubClient(config)
        
        # CSV file path for recording uploads
        self.csv_file_path = 'upload_results.csv'
        self._initialize_csv()
        
        # Processing statistics
        self.stats = {
            'total_processed': 0,
            'successful_uploads': 0,
            'failed_uploads': 0,
            'skipped_images': 0,
            'start_time': None,
            'end_time': None
        }
        
        self.logger.info("ImageProcessor initialized successfully")
    
    def _initialize_csv(self) -> None:
        """Initialize CSV file with headers if it doesn't exist."""
        try:
            # Check if file exists
            file_exists = os.path.exists(self.csv_file_path)
            
            if not file_exists:
                with open(self.csv_file_path, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['image_id', 'cafe_name', 'filename', 'github_url', 'upload_timestamp'])
                self.logger.info(f"Created new CSV file: {self.csv_file_path}")
            else:
                self.logger.info(f"Using existing CSV file: {self.csv_file_path}")
        except Exception as e:
            self.logger.error(f"Error initializing CSV file: {str(e)}")
            raise
    
    def _save_to_csv(self, image_id: int, image_record: Dict[str, Any], filename: str, github_url: str) -> bool:
        """
        Save upload result to CSV file.
        
        Args:
            image_id: ID of the image
            image_record: Full image record from database
            filename: Generated filename
            github_url: GitHub URL of uploaded image
            
        Returns:
            True if save was successful, False otherwise
        """
        try:
            # Extract cafe name from image record
            cafe_name = image_record.get('cafe_name', 'Unknown')
            
            # Get current timestamp
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Append to CSV file
            with open(self.csv_file_path, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([image_id, cafe_name, filename, github_url, timestamp])
            
            self.logger.info(f"Saved to CSV: ID={image_id}, Cafe={cafe_name}, File={filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error saving to CSV for image ID {image_id}: {str(e)}")
            return False
    
    def request_shutdown(self) -> None:
        """Request shutdown of the image processor."""
        self._shutdown_requested = True
        self.logger.info("Shutdown requested for ImageProcessor")
    
    def process_all_images(self, 
                          batch_size: int = 100,
                          max_images: Optional[int] = None,
                          skip_existing: bool = True) -> Dict[str, Any]:
        """
        Process all images from the database in batches.
        
        Args:
            batch_size: Number of images to process in each batch
            max_images: Maximum number of images to process (None for all)
            skip_existing: Whether to skip images that already have GitHub paths
            
        Returns:
            Dictionary containing processing statistics
            
        Raises:
            ImageProcessingError: If processing fails
        """
        self.logger.info(f"Starting batch processing of all images (batch_size={batch_size})")
        self.stats['start_time'] = datetime.now()
        
        try:
            # Get total count for progress tracking
            total_count = self.db_manager.get_images_count()
            if max_images:
                total_count = min(total_count, max_images)
            
            self.logger.info(f"Found {total_count} images to process")
            
            if total_count == 0:
                self.logger.info("No images found to process")
                return self._get_final_stats()
            
            # Create progress tracker
            progress_tracker = get_logger_manager().create_progress_tracker(
                total_count, "Image Upload"
            )
            
            # Process images in batches
            offset = 0
            processed_count = 0
            
            while processed_count < total_count:
                # Calculate batch size for this iteration
                current_batch_size = min(batch_size, total_count - processed_count)
                if max_images:
                    current_batch_size = min(current_batch_size, max_images - processed_count)
                
                # Get batch of images
                images = self.db_manager.get_images(
                    limit=current_batch_size,
                    offset=offset
                )
                
                if not images:
                    break
                
                # Process each image in the batch
                for image_record in images:
                    # Check for shutdown request
                    if self._shutdown_requested:
                        self.logger.info("Shutdown requested, stopping image processing")
                        return self._get_final_stats()
                    
                    try:
                        # Skip if image already has GitHub URL and skip_existing is True
                        image_url = image_record.get('image_url', '')
                        if skip_existing and image_url.startswith(('http://', 'https://')):
                            self.logger.info(
                                f"Skipping image ID {image_record['id']} - already has GitHub URL: "
                                f"{image_url}"
                            )
                            self.stats['skipped_images'] += 1
                            progress_tracker.update()
                            continue
                        
                        # Process single image
                        success = self.process_single_image(image_record)
                        
                        if success:
                            self.stats['successful_uploads'] += 1
                        else:
                            self.stats['failed_uploads'] += 1
                        
                        self.stats['total_processed'] += 1
                        progress_tracker.update()
                        
                        # Log progress every 10 images
                        if progress_tracker.current % 10 == 0:
                            self.logger.progress(progress_tracker)
                        
                        # Check if we've reached max_images limit
                        if max_images and processed_count >= max_images:
                            break
                            
                    except Exception as e:
                        self.logger.error(
                            f"Unexpected error processing image ID {image_record.get('id', 'unknown')}: {str(e)}"
                        )
                        self.stats['failed_uploads'] += 1
                        self.stats['total_processed'] += 1
                        progress_tracker.update()
                
                processed_count += len(images)
                offset += current_batch_size
                
                # Break if we've processed enough images
                if max_images and processed_count >= max_images:
                    break
            
            # Final progress update
            self.logger.progress(progress_tracker)
            
        except DatabaseError as e:
            raise ImageProcessingError(f"Database error during batch processing: {str(e)}")
        except Exception as e:
            raise ImageProcessingError(f"Unexpected error during batch processing: {str(e)}")
        finally:
            self.stats['end_time'] = datetime.now()
        
        return self._get_final_stats()
    
    def process_single_image(self, image_record: Dict[str, Any], retry_count: int = 3) -> bool:
        """
        Process a single image record with comprehensive error handling and retry logic.
        
        Args:
            image_record: Dictionary containing image data and metadata
            retry_count: Number of retry attempts for recoverable errors
            
        Returns:
            True if processing was successful, False otherwise
        """
        image_id = image_record.get('id')
        original_url = image_record.get('image_url')
        
        for attempt in range(retry_count):
            try:
                self.logger.info(f"Processing image ID {image_id} (attempt {attempt + 1}/{retry_count})")
                
                # Validate image record
                if not self._validate_image_record(image_record):
                    self.logger.error(f"Image record validation failed for ID {image_id}")
                    return False
                
                # Get image url from database record
                image_url = image_record.get('image_url')
                
                if not image_url:
                    self.logger.error(f"No image url found for ID {image_id}")
                    return False
                
                # Load image data (from URL or local file path)
                if image_url.startswith(('http://', 'https://')):
                    self.logger.info(f"Loading image from URL for ID {image_id}: {image_url}")
                    image_data = self.load_image_from_url(image_url)
                else:
                    self.logger.info(f"Loading image from local path for ID {image_id}: {image_url}")
                    image_data = self.load_image_from_path(image_url)
                
                if image_data is None:
                    self.logger.error(f"Failed to load image for ID {image_id}: {image_url}")
                    return False
                
                # Generate filename (after image_data is loaded)
                filename = self._generate_filename(image_record, image_data)
                
                # Validate image data size and format
                if len(image_data) == 0:
                    self.logger.error(f"Empty image data for ID {image_id}")
                    return False
                elif len(image_data) < 10:  # Too small to be a valid image
                    self.logger.error(f"Image data too small for ID {image_id}: {len(image_data)} bytes")
                    return False
                elif len(image_data) > 100 * 1024 * 1024:  # 100MB limit
                    self.logger.error(f"Image data too large for ID {image_id}: {len(image_data)} bytes")
                    return False
                
                # Validate that it looks like image data by checking magic bytes
                if not self._is_valid_image_data(image_data):
                    self.logger.error(f"Data for ID {image_id} does not appear to be valid image data")
                    return False
                
                # Store original database state for potential rollback
                db_rollback_needed = False
                
                try:
                    # Upload to GitHub with error handling
                    upload_result = self.github_client.upload_image(
                        filename=filename,
                        image_data=image_data
                    )
                    
                    # Extract GitHub path from upload result
                    github_path = upload_result['download_url']
                    
                    # Save to CSV instead of updating database
                    csv_success = self._save_to_csv(image_id, image_record, filename, github_path)
                    
                    if csv_success:
                        self.logger.info(
                            f"Successfully processed image ID {image_id}: {filename} -> {github_path}"
                        )
                        return True
                    else:
                        # CSV save failed, but GitHub upload succeeded
                        self.logger.error(
                            f"CRITICAL: GitHub upload succeeded but CSV save failed for ID {image_id}. "
                            f"Manual intervention may be required. GitHub path: {github_path}"
                        )
                        return False
                        
                except GitHubAPIError as github_error:
                    # GitHub upload failed, no database changes needed
                    error_msg = str(github_error)
                    
                    # Check if this is a retryable GitHub error
                    retryable_github_errors = [
                        'timeout', 'connection', 'server error', '500', '502', '503', '504'
                    ]
                    
                    is_retryable = any(keyword in error_msg.lower() for keyword in retryable_github_errors)
                    
                    if is_retryable and attempt < retry_count - 1:
                        wait_time = 2 ** attempt  # Exponential backoff
                        self.logger.warning(
                            f"Retryable GitHub error for ID {image_id} (attempt {attempt + 1}): {error_msg}. "
                            f"Retrying in {wait_time} seconds..."
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"GitHub API error for image ID {image_id}: {error_msg}")
                        return False
                
            except AuthenticationError as e:
                # Authentication errors are not retryable
                self.logger.error(f"GitHub authentication error for image ID {image_id}: {str(e)}")
                return False
                
            except RateLimitError as e:
                # Rate limit errors should be handled by the GitHub client, but if we get here, log and fail
                self.logger.error(f"Rate limit error not handled by client for image ID {image_id}: {str(e)}")
                return False
                
            except DatabaseError as e:
                error_msg = str(e)
                
                # Check if this is a retryable database error
                retryable_db_errors = [
                    'connection', 'timeout', 'lock wait', 'deadlock', 'server has gone away'
                ]
                
                is_retryable = any(keyword in error_msg.lower() for keyword in retryable_db_errors)
                
                if is_retryable and attempt < retry_count - 1:
                    wait_time = 1.5 ** attempt  # Gentler backoff for database
                    self.logger.warning(
                        f"Retryable database error for ID {image_id} (attempt {attempt + 1}): {error_msg}. "
                        f"Retrying in {wait_time:.1f} seconds..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Database error for image ID {image_id}: {error_msg}")
                    return False
                    
            except Exception as e:
                # Unexpected errors
                error_msg = str(e)
                self.logger.error(f"Unexpected error processing image ID {image_id} (attempt {attempt + 1}): {error_msg}")
                
                # For unexpected errors, only retry if we have attempts left
                if attempt < retry_count - 1:
                    wait_time = 1.0 * (attempt + 1)  # Linear backoff for unexpected errors
                    self.logger.warning(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    return False
        
        # If we get here, all retry attempts failed
        self.logger.error(f"Failed to process image ID {image_id} after {retry_count} attempts")
        return False
    
    def _validate_image_record(self, image_record: Dict[str, Any]) -> bool:
        """
        Validate that an image record has all required fields.
        
        Args:
            image_record: Image record dictionary
            
        Returns:
            True if valid, False otherwise
        """
        # Debug: log the actual record structure
        self.logger.info(f"Validating image record: {list(image_record.keys())}")
        
        required_fields = ['id', 'image_url']
        
        for field in required_fields:
            if field not in image_record or image_record[field] is None:
                self.logger.error(f"Image record missing required field: {field}. Available fields: {list(image_record.keys())}")
                return False
        
        # Validate image url is not empty
        image_url = image_record['image_url']
        if not image_url or (isinstance(image_url, str) and len(image_url.strip()) == 0):
            self.logger.error(f"Image record ID {image_record['id']} has empty image url")
            return False
        
        return True
    
    def _generate_filename(self, image_record: Dict[str, Any], image_data: bytes = None) -> str:
        """
        Generate a unique filename for the image.
        
        Args:
            image_record: Image record dictionary
            image_data: Optional binary image data for hash generation
            
        Returns:
            Generated filename with extension
        """
        image_id = image_record['id']
        image_url = image_record['image_url']
        
        # Try to get extension from original path first
        original_path = Path(image_url)
        original_extension = original_path.suffix.lower().lstrip('.')
        
        # Create hash for uniqueness
        if image_data:
            # Use image data for hash if available
            hash_object = hashlib.md5(image_data)
            file_hash = hash_object.hexdigest()[:8]
            
            # Detect extension from image data
            detected_extension = self._detect_image_extension(image_data)
            extension = detected_extension if detected_extension != 'jpg' else (original_extension or 'jpg')
        else:
            # Use image url for hash if no data available
            hash_object = hashlib.md5(image_url.encode('utf-8'))
            file_hash = hash_object.hexdigest()[:8]
            extension = original_extension or 'jpg'
        
        # Generate filename: image_{id}_{hash}.{ext}
        filename = f"image_{image_id}_{file_hash}.{extension}"
        
        self.logger.info(f"Generated filename for image ID {image_id}: {filename}")
        return filename
    
    def _is_valid_image_data(self, image_data: bytes) -> bool:
        """
        Check if the data appears to be valid image data by examining magic bytes.
        
        Args:
            image_data: Binary image data to validate
            
        Returns:
            True if data appears to be a valid image, False otherwise
        """
        if not isinstance(image_data, bytes) or len(image_data) < 4:
            return False
        
        # Check magic bytes for common image formats
        magic_bytes = [
            b'\xff\xd8\xff',  # JPEG
            b'\x89PNG\r\n\x1a\n',  # PNG
            b'GIF87a',  # GIF87a
            b'GIF89a',  # GIF89a
            b'RIFF',  # WEBP (also check for WEBP in first 12 bytes)
            b'BM',  # BMP
            b'\x00\x00\x01\x00',  # ICO
            b'\x00\x00\x02\x00',  # CUR
        ]
        
        # Check standard magic bytes
        for magic in magic_bytes:
            if image_data.startswith(magic):
                # Special case for WEBP - need to check for WEBP signature
                if magic == b'RIFF' and len(image_data) >= 12:
                    return b'WEBP' in image_data[:12]
                return True
        
        return False
    
    def _detect_image_extension(self, image_data: bytes) -> str:
        """
        Detect image file extension from binary data.
        
        Args:
            image_data: Binary image data
            
        Returns:
            File extension (without dot)
        """
        # Convert to bytes if it's a base64 string
        if isinstance(image_data, str):
            try:
                image_data = base64.b64decode(image_data)
            except:
                return 'jpg'  # Default fallback
        
        # Check magic bytes for common image formats
        if image_data.startswith(b'\xff\xd8\xff'):
            return 'jpg'
        elif image_data.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'png'
        elif image_data.startswith(b'GIF87a') or image_data.startswith(b'GIF89a'):
            return 'gif'
        elif image_data.startswith(b'RIFF') and b'WEBP' in image_data[:12]:
            return 'webp'
        elif image_data.startswith(b'BM'):
            return 'bmp'
        elif image_data.startswith(b'\x00\x00\x01\x00'):
            return 'ico'
        else:
            # Default to jpg if we can't detect the format
            return 'jpg'
    
    def load_image_from_url(self, image_url: str) -> Optional[bytes]:
        """
        Load image data from URL and return as bytes.
        
        Args:
            image_url: URL to the image file
            
        Returns:
            Image data as bytes, or None if loading fails
        """
        try:
            self.logger.info(f"Downloading image from URL: {image_url}")
            
            # Set up headers to mimic a browser request
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            # Download the image with timeout
            response = requests.get(image_url, headers=headers, timeout=30)
            response.raise_for_status()  # Raise an exception for bad status codes
            
            image_data = response.content
            
            # Check file size (limit to 100MB)
            if len(image_data) > 100 * 1024 * 1024:
                self.logger.error(f"Image file too large: {image_url} ({len(image_data)} bytes)")
                return None
            
            if len(image_data) == 0:
                self.logger.error(f"Image file is empty: {image_url}")
                return None
            
            # Validate that it's actually image data
            if not self._is_valid_image_data(image_data):
                self.logger.error(f"URL does not contain valid image data: {image_url}")
                return None
            
            self.logger.info(f"Successfully downloaded image from URL: {image_url} ({len(image_data)} bytes)")
            return image_data
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network error downloading image from URL {image_url}: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error downloading image from URL {image_url}: {str(e)}")
            return None

    def load_image_from_path(self, image_path: str) -> Optional[bytes]:
        """
        Load image data from file path and return as bytes.
        
        Args:
            image_path: Path to the image file (local file path)
            
        Returns:
            Image data as bytes, or None if loading fails
        """
        try:
            # Convert to Path object for better handling
            path = Path(image_path)
            
            # Check if file exists
            if not path.exists():
                self.logger.error(f"Image file not found: {image_path}")
                return None
            
            # Check if it's a file (not directory)
            if not path.is_file():
                self.logger.error(f"Path is not a file: {image_path}")
                return None
            
            # Check file size (limit to 100MB)
            file_size = path.stat().st_size
            if file_size > 100 * 1024 * 1024:
                self.logger.error(f"Image file too large: {image_path} ({file_size} bytes)")
                return None
            
            if file_size == 0:
                self.logger.error(f"Image file is empty: {image_path}")
                return None
            
            # Read the file
            with open(path, 'rb') as f:
                image_data = f.read()
            
            # Validate that it's actually image data
            if not self._is_valid_image_data(image_data):
                self.logger.error(f"File does not contain valid image data: {image_path}")
                return None
            
            self.logger.info(f"Successfully loaded image from path: {image_path} ({len(image_data)} bytes)")
            return image_data
            
        except PermissionError:
            self.logger.error(f"Permission denied reading image file: {image_path}")
            return None
        except OSError as e:
            self.logger.error(f"OS error reading image file {image_path}: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error reading image file {image_path}: {str(e)}")
            return None
    
    def convert_image_to_base64(self, image_data: bytes) -> str:
        """
        Convert image bytes to base64 string.
        
        Args:
            image_data: Image data as bytes
            
        Returns:
            Base64 encoded string of the image data
        """
        try:
            if not isinstance(image_data, bytes):
                raise ValueError("Image data must be bytes")
            
            if len(image_data) == 0:
                raise ValueError("Image data is empty")
            
            # Convert to base64
            base64_string = base64.b64encode(image_data).decode('utf-8')
            
            self.logger.info(f"Successfully converted {len(image_data)} bytes to base64 ({len(base64_string)} characters)")
            return base64_string
            
        except Exception as e:
            self.logger.error(f"Error converting image to base64: {str(e)}")
            raise
    
    def load_and_convert_image_path(self, image_path: str) -> Optional[str]:
        """
        Load image from file path and convert to base64 string.
        
        Args:
            image_path: Path to the image file
            
        Returns:
            Base64 encoded string of the image, or None if loading/conversion fails
        """
        try:
            # Load image data from path
            image_data = self.load_image_from_path(image_path)
            
            if image_data is None:
                return None
            
            # Convert to base64
            base64_string = self.convert_image_to_base64(image_data)
            
            self.logger.info(f"Successfully loaded and converted image from path: {image_path}")
            return base64_string
            
        except Exception as e:
            self.logger.error(f"Error loading and converting image from path {image_path}: {str(e)}")
            return None
    
    def process_image_by_id(self, image_id: int) -> bool:
        """
        Process a specific image by its ID.
        
        Args:
            image_id: ID of the image to process
            
        Returns:
            True if processing was successful, False otherwise
            
        Raises:
            ImageProcessingError: If image is not found or processing fails
        """
        try:
            # Get image record from database
            image_record = self.db_manager.get_image_by_id(image_id)
            
            if not image_record:
                raise ImageProcessingError(f"Image with ID {image_id} not found in database")
            
            # Process the image
            return self.process_single_image(image_record)
            
        except DatabaseError as e:
            raise ImageProcessingError(f"Database error retrieving image ID {image_id}: {str(e)}")
    
    def convert_image_paths_to_base64(self, 
                                    limit: Optional[int] = None, 
                                    offset: int = 0,
                                    update_database: bool = False) -> Dict[str, Any]:
        """
        Convert images stored as file paths to base64 format.
        
        Args:
            limit: Maximum number of records to process (None for all)
            offset: Number of records to skip
            update_database: Not supported (only PATH_COLUMN available)
            
        Returns:
            Dictionary containing conversion results
        """
        results = {
            'total_processed': 0,
            'successful_conversions': 0,
            'failed_conversions': 0,
            'skipped_records': 0,
            'errors': []
        }
        
        try:
            # Get images from database
            images = self.db_manager.get_images(limit=limit, offset=offset)
            
            if not images:
                self.logger.info("No images found to process")
                return results
            
            self.logger.info(f"Processing {len(images)} images for path-to-base64 conversion")
            
            for image_record in images:
                image_id = image_record.get('id')
                image_url = image_record.get('image_url')
                
                results['total_processed'] += 1
                
                try:
                    if not image_url:
                        results['skipped_records'] += 1
                        self.logger.debug(f"Skipping ID {image_id} - no image url")
                        continue
                    
                    # Process both URLs and local paths
                    self.logger.info(f"Converting image to base64 for ID {image_id}: {image_url}")
                    
                    # Load and convert image (from URL or local path)
                    if image_url.startswith(('http://', 'https://')):
                        image_data = self.load_image_from_url(image_url)
                    else:
                        image_data = self.load_image_from_path(image_url)
                    
                    if image_data:
                        base64_data = self.convert_image_to_base64(image_data)
                    else:
                        base64_data = None
                    
                    if base64_data:
                        results['successful_conversions'] += 1
                        
                        # Note: Database update is not supported since we only have PATH_COLUMN
                        # The base64 data would need to be stored elsewhere or used directly
                        self.logger.info(f"Base64 conversion successful for ID {image_id} (length: {len(base64_data)})")
                        
                        if update_database:
                            self.logger.warning(f"Database update requested but not supported - only PATH_COLUMN available for ID {image_id}")
                            results['errors'].append(f"ID {image_id}: Database update not supported - no image data column")
                    else:
                        results['failed_conversions'] += 1
                        error_msg = f"Failed to convert image path to base64 for ID {image_id}: {image_url}"
                        results['errors'].append(error_msg)
                        
                except Exception as e:
                    results['failed_conversions'] += 1
                    error_msg = f"Error processing image ID {image_id}: {str(e)}"
                    results['errors'].append(error_msg)
                    self.logger.error(error_msg)
            
            # Log summary
            self.logger.info(
                f"Path-to-base64 conversion completed: "
                f"{results['successful_conversions']} successful, "
                f"{results['failed_conversions']} failed, "
                f"{results['skipped_records']} skipped"
            )
            
        except Exception as e:
            error_msg = f"Error during path-to-base64 conversion: {str(e)}"
            results['errors'].append(error_msg)
            self.logger.error(error_msg)
        
        return results
    
    def get_image_as_base64(self, image_id: int) -> Optional[str]:
        """
        Get a specific image as base64 string, loading from path.
        
        Args:
            image_id: ID of the image to retrieve
            
        Returns:
            Base64 encoded string of the image, or None if not found/failed
        """
        try:
            # Get image record from database
            image_record = self.db_manager.get_image_by_id(image_id)
            
            if not image_record:
                self.logger.error(f"Image with ID {image_id} not found in database")
                return None
            
            image_url = image_record.get('image_url')
            
            if not image_url:
                self.logger.error(f"No image url found for ID {image_id}")
                return None
            
            # Load from URL or file path and convert to base64
            if image_url.startswith(('http://', 'https://')):
                image_data = self.load_image_from_url(image_url)
            else:
                image_data = self.load_image_from_path(image_url)
            
            if image_data:
                return self.convert_image_to_base64(image_data)
            else:
                return None
                
        except Exception as e:
            self.logger.error(f"Error getting image as base64 for ID {image_id}: {str(e)}")
            return None
    
    def get_processing_stats(self) -> Dict[str, Any]:
        """
        Get current processing statistics.
        
        Returns:
            Dictionary containing processing statistics
        """
        return self.stats.copy()
    
    def _get_final_stats(self) -> Dict[str, Any]:
        """
        Get final processing statistics with calculated metrics.
        
        Returns:
            Dictionary containing final processing statistics
        """
        stats = self.stats.copy()
        
        if stats['start_time'] and stats['end_time']:
            duration = stats['end_time'] - stats['start_time']
            stats['duration_seconds'] = duration.total_seconds()
            stats['duration_formatted'] = str(duration)
            
            # Calculate processing rate
            if stats['duration_seconds'] > 0:
                stats['images_per_second'] = stats['total_processed'] / stats['duration_seconds']
            else:
                stats['images_per_second'] = 0
        
        # Calculate success rate
        if stats['total_processed'] > 0:
            stats['success_rate'] = (stats['successful_uploads'] / stats['total_processed']) * 100
        else:
            stats['success_rate'] = 0
        
        # Log final statistics
        self.logger.info("=== Processing Complete ===")
        self.logger.info(f"Total processed: {stats['total_processed']}")
        self.logger.info(f"Successful uploads: {stats['successful_uploads']}")
        self.logger.info(f"Failed uploads: {stats['failed_uploads']}")
        self.logger.info(f"Skipped images: {stats['skipped_images']}")
        self.logger.info(f"Success rate: {stats['success_rate']:.1f}%")
        if 'duration_formatted' in stats:
            self.logger.info(f"Duration: {stats['duration_formatted']}")
            self.logger.info(f"Processing rate: {stats['images_per_second']:.2f} images/second")
        
        return stats
    
    def recover_failed_uploads(self, failed_image_ids: List[int], max_retries: int = 2) -> Dict[str, Any]:
        """
        Attempt to recover failed uploads by retrying them with enhanced error handling.
        
        Args:
            failed_image_ids: List of image IDs that failed to upload
            max_retries: Maximum number of retry attempts per image
            
        Returns:
            Dictionary with recovery results
        """
        recovery_stats = {
            'attempted': len(failed_image_ids),
            'recovered': 0,
            'still_failed': 0,
            'errors': []
        }
        
        if not failed_image_ids:
            return recovery_stats
        
        self.logger.info(f"Starting recovery process for {len(failed_image_ids)} failed uploads")
        
        for image_id in failed_image_ids:
            try:
                # Get fresh image record from database
                image_record = self.db_manager.get_image_by_id(image_id)
                
                if not image_record:
                    error_msg = f"Image ID {image_id} not found in database during recovery"
                    self.logger.error(error_msg)
                    recovery_stats['errors'].append(error_msg)
                    recovery_stats['still_failed'] += 1
                    continue
                
                # Attempt recovery with reduced retry count to avoid infinite loops
                success = self.process_single_image(image_record, retry_count=max_retries)
                
                if success:
                    recovery_stats['recovered'] += 1
                    self.logger.info(f"Successfully recovered upload for image ID {image_id}")
                else:
                    recovery_stats['still_failed'] += 1
                    error_msg = f"Recovery failed for image ID {image_id}"
                    self.logger.warning(error_msg)
                    recovery_stats['errors'].append(error_msg)
                
                # Small delay between recovery attempts
                time.sleep(0.5)
                
            except Exception as e:
                error_msg = f"Unexpected error during recovery of image ID {image_id}: {str(e)}"
                self.logger.error(error_msg)
                recovery_stats['errors'].append(error_msg)
                recovery_stats['still_failed'] += 1
        
        # Log recovery results
        recovery_rate = (recovery_stats['recovered'] / recovery_stats['attempted']) * 100 if recovery_stats['attempted'] > 0 else 0
        self.logger.info(
            f"Recovery completed: {recovery_stats['recovered']}/{recovery_stats['attempted']} recovered "
            f"({recovery_rate:.1f}% recovery rate)"
        )
        
        return recovery_stats
    
    def validate_upload_integrity(self, image_ids: List[int]) -> Dict[str, Any]:
        """
        Validate the integrity of uploaded images by checking database consistency.
        
        Args:
            image_ids: List of image IDs to validate
            
        Returns:
            Dictionary with validation results
        """
        validation_results = {
            'total_checked': len(image_ids),
            'valid': 0,
            'invalid': 0,
            'missing_paths': 0,
            'issues': []
        }
        
        self.logger.info(f"Validating upload integrity for {len(image_ids)} images")
        
        for image_id in image_ids:
            try:
                image_record = self.db_manager.get_image_by_id(image_id)
                
                if not image_record:
                    issue = f"Image ID {image_id} not found in database"
                    validation_results['issues'].append(issue)
                    validation_results['invalid'] += 1
                    continue
                
                image_url = image_record.get('image_url')
                
                if not image_url:
                    validation_results['missing_paths'] += 1
                    validation_results['issues'].append(f"Image ID {image_id} has no image URL")
                elif not image_url.startswith('http'):
                    validation_results['invalid'] += 1
                    validation_results['issues'].append(f"Image ID {image_id} has local path instead of GitHub URL: {image_url}")
                else:
                    validation_results['valid'] += 1
                    
            except Exception as e:
                issue = f"Error validating image ID {image_id}: {str(e)}"
                validation_results['issues'].append(issue)
                validation_results['invalid'] += 1
        
        # Log validation summary
        self.logger.info(
            f"Validation completed: {validation_results['valid']} valid, "
            f"{validation_results['invalid']} invalid, "
            f"{validation_results['missing_paths']} missing paths"
        )
        
        return validation_results
    
    def get_failed_uploads_summary(self) -> Dict[str, Any]:
        """
        Get a summary of images that may have failed uploads based on database state.
        
        Returns:
            Dictionary with failed uploads summary
        """
        try:
            # Query for images without GitHub paths (potential failures)
            images_without_paths = self.db_manager.get_images(limit=None)
            failed_candidates = [
                img for img in images_without_paths 
                if not img.get('image_url') or not img['image_url'].startswith('http')
            ]
            
            summary = {
                'total_images': len(images_without_paths),
                'images_without_paths': len(failed_candidates),
                'failed_image_ids': [img['id'] for img in failed_candidates],
                'failure_rate': (len(failed_candidates) / len(images_without_paths)) * 100 if images_without_paths else 0
            }
            
            self.logger.info(
                f"Failed uploads summary: {summary['images_without_paths']}/{summary['total_images']} "
                f"images without valid paths ({summary['failure_rate']:.1f}% failure rate)"
            )
            
            return summary
            
        except Exception as e:
            self.logger.error(f"Error generating failed uploads summary: {str(e)}")
            return {
                'total_images': 0,
                'images_without_paths': 0,
                'failed_image_ids': [],
                'failure_rate': 0,
                'error': str(e)
            }
    
    def close(self) -> None:
        """Close all resources and connections."""
        try:
            if hasattr(self, 'github_client'):
                self.github_client.close()
            if hasattr(self, 'db_manager'):
                self.db_manager.close()
            self.logger.info("ImageProcessor resources closed successfully")
        except Exception as e:
            self.logger.error(f"Error closing ImageProcessor resources: {str(e)}")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()