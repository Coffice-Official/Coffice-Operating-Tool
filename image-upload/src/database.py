"""
Database operations module for GitHub Image Uploader.

This module handles MySQL database connections and operations
for retrieving and updating image records.
"""

import logging
import time
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Generator, Tuple
import mysql.connector
from mysql.connector import pooling, Error as MySQLError
from mysql.connector.pooling import MySQLConnectionPool

from .config import Config


class DatabaseError(Exception):
    """Raised when database operations fail."""
    pass


class DatabaseManager:
    """Manages MySQL database connections and operations."""
    
    def __init__(self, config: Config):
        """
        Initialize DatabaseManager with configuration.
        
        Args:
            config: Configuration instance with database settings
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._connection_pool: Optional[MySQLConnectionPool] = None
        self._max_retries = 3
        self._retry_delay = 1.0  # seconds
        
        # Initialize connection pool
        self._create_connection_pool()
    
    def _create_connection_pool(self) -> None:
        """Create MySQL connection pool with retry logic."""
        pool_config = {
            **self.config.get_mysql_connection_params(),
            'pool_name': 'github_uploader_pool',
            'pool_size': 5,
            'pool_reset_session': True,
            'autocommit': False,
            'charset': 'utf8mb4',
            'collation': 'utf8mb4_unicode_ci',
            'time_zone': '+00:00',
            'sql_mode': 'STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO',
        }
        
        for attempt in range(self._max_retries):
            try:
                self._connection_pool = pooling.MySQLConnectionPool(**pool_config)
                self.logger.info("Database connection pool created successfully")
                
                # Test the connection
                self._test_connection()
                return
                
            except MySQLError as e:
                self.logger.warning(
                    f"Failed to create connection pool (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))  # Exponential backoff
                else:
                    raise DatabaseError(f"Failed to create database connection pool after {self._max_retries} attempts: {e}")
    
    def _test_connection(self) -> None:
        """Test database connection and validate table structure."""
        with self.get_connection() as connection:
            cursor = connection.cursor()
            
            # Test basic connectivity
            cursor.execute("SELECT 1")
            cursor.fetchone()
            
            # Validate that the required table exists
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (self.config.mysql_database, self.config.image_table)
            )
            
            if cursor.fetchone()[0] == 0:
                raise DatabaseError(
                    f"Table '{self.config.image_table}' does not exist in database '{self.config.mysql_database}'"
                )
            
            # Validate that required columns exist
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name IN (%s, %s)",
                (
                    self.config.mysql_database,
                    self.config.image_table,
                    'place_image_id',
                    self.config.path_column
                )
            )
            
            existing_columns = {row[0] for row in cursor.fetchall()}
            required_columns = {'place_image_id', self.config.path_column}
            missing_columns = required_columns - existing_columns
            
            if missing_columns:
                raise DatabaseError(
                    f"Missing required columns in table '{self.config.image_table}': {', '.join(missing_columns)}"
                )
            
            self.logger.info("Database connection and table structure validated successfully")
    
    @contextmanager
    def get_connection(self) -> Generator[mysql.connector.MySQLConnection, None, None]:
        """
        Get a database connection from the pool.
        
        Yields:
            MySQL connection instance
            
        Raises:
            DatabaseError: If unable to get connection from pool
        """
        if not self._connection_pool:
            raise DatabaseError("Connection pool not initialized")
        
        connection = None
        try:
            connection = self._connection_pool.get_connection()
            yield connection
            
        except MySQLError as e:
            if connection:
                try:
                    connection.rollback()
                except MySQLError:
                    pass  # Ignore rollback errors
            raise DatabaseError(f"Database operation failed: {e}")
            
        finally:
            if connection and connection.is_connected():
                connection.close()
    
    def get_images(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """
        Retrieve image records from the database.
        
        Args:
            limit: Maximum number of records to retrieve (None for all)
            offset: Number of records to skip
            
        Returns:
            List of image record dictionaries
            
        Raises:
            DatabaseError: If query fails
        """
        query = f"""
            SELECT 
                pi.place_image_id as id,
                pi.{self.config.path_column} as image_url,
                p.name as cafe_name
            FROM {self.config.image_table} pi
            LEFT JOIN place p ON pi.place_id = p.place_id
            WHERE pi.{self.config.path_column} IS NOT NULL
        """
        
        params = []
        
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
            
        if offset > 0:
            query += " OFFSET %s"
            params.append(offset)
        
        for attempt in range(self._max_retries):
            try:
                with self.get_connection() as connection:
                    cursor = connection.cursor(dictionary=True)
                    cursor.execute(query, params)
                    results = cursor.fetchall()
                    
                    self.logger.info(f"Retrieved {len(results)} image records from database")
                    return results
                    
            except MySQLError as e:
                self.logger.warning(
                    f"Failed to retrieve images (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))
                else:
                    raise DatabaseError(f"Failed to retrieve images after {self._max_retries} attempts: {e}")
    

    def update_image_path(self, image_id: int, new_path: str) -> bool:
        """
        Update the image path for a specific record with transaction rollback support.
        
        Args:
            image_id: ID of the image record to update
            new_path: New GitHub path for the image
            
        Returns:
            True if update was successful, False otherwise
            
        Raises:
            DatabaseError: If update fails after all retries
        """
        query = f"""
            UPDATE {self.config.image_table}
            SET {self.config.path_column} = %s
            WHERE place_image_id = %s
        """
        
        for attempt in range(self._max_retries):
            connection = None
            try:
                with self.get_connection() as connection:
                    # Start transaction explicitly
                    connection.start_transaction()
                    
                    cursor = connection.cursor()
                    
                    # First, verify the record exists and get current state
                    verify_query = f"SELECT place_image_id, {self.config.path_column} FROM {self.config.image_table} WHERE place_image_id = %s FOR UPDATE"
                    cursor.execute(verify_query, (image_id,))
                    current_record = cursor.fetchone()
                    
                    if not current_record:
                        connection.rollback()
                        self.logger.warning(f"No record found with ID {image_id} for update")
                        return False
                    
                    # Log the current state for rollback reference
                    current_path = current_record[1] if len(current_record) > 1 else None
                    self.logger.info(f"Updating image ID {image_id}: '{current_path}' -> '{new_path}'")
                    
                    # Perform the update
                    cursor.execute(query, (new_path, image_id))
                    
                    if cursor.rowcount == 0:
                        connection.rollback()
                        self.logger.warning(f"Update affected 0 rows for ID {image_id}")
                        return False
                    
                    # Verify the update was successful
                    cursor.execute(f"SELECT {self.config.path_column} FROM {self.config.image_table} WHERE place_image_id = %s", (image_id,))
                    updated_record = cursor.fetchone()
                    
                    if not updated_record or updated_record[0] != new_path:
                        connection.rollback()
                        self.logger.error(f"Update verification failed for ID {image_id}")
                        raise DatabaseError(f"Update verification failed for ID {image_id}")
                    
                    # Commit the transaction
                    connection.commit()
                    self.logger.info(f"Successfully updated image path for ID {image_id}: {new_path}")
                    return True
                    
            except MySQLError as e:
                # Attempt to rollback if connection is available
                if connection:
                    try:
                        connection.rollback()
                        self.logger.info(f"Transaction rolled back for image ID {image_id}")
                    except MySQLError as rollback_error:
                        self.logger.error(f"Failed to rollback transaction for ID {image_id}: {rollback_error}")
                
                error_code = getattr(e, 'errno', None)
                error_msg = str(e)
                
                # Log detailed error information
                self.logger.warning(
                    f"Database error updating image ID {image_id} (attempt {attempt + 1}/{self._max_retries}): "
                    f"Error {error_code}: {error_msg}"
                )
                
                # Check if this is a retryable error
                retryable_errors = [
                    1205,  # Lock wait timeout
                    1213,  # Deadlock found
                    2003,  # Can't connect to MySQL server
                    2006,  # MySQL server has gone away
                    2013,  # Lost connection to MySQL server
                ]
                
                if error_code in retryable_errors and attempt < self._max_retries - 1:
                    wait_time = self._retry_delay * (2 ** attempt)
                    self.logger.info(f"Retryable error detected, waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                    continue
                elif attempt < self._max_retries - 1:
                    # For other errors, still retry but with shorter delay
                    wait_time = self._retry_delay
                    self.logger.info(f"Retrying update for ID {image_id} in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise DatabaseError(
                        f"Failed to update image path for ID {image_id} after {self._max_retries} attempts. "
                        f"Last error (code {error_code}): {error_msg}"
                    )
            except Exception as e:
                # Handle non-MySQL exceptions
                if connection:
                    try:
                        connection.rollback()
                        self.logger.info(f"Transaction rolled back for image ID {image_id} due to unexpected error")
                    except:
                        pass
                
                self.logger.error(f"Unexpected error updating image ID {image_id}: {str(e)}")
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay)
                    continue
                else:
                    raise DatabaseError(f"Unexpected error updating image ID {image_id}: {str(e)}")
        
        # This should never be reached due to the loop logic
        return False
    
    def get_image_by_id(self, image_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieve a specific image record by ID.
        
        Args:
            image_id: ID of the image record to retrieve
            
        Returns:
            Image record dictionary or None if not found
            
        Raises:
            DatabaseError: If query fails
        """
        query = f"""
            SELECT 
                pi.place_image_id as id,
                pi.{self.config.path_column} as image_url,
                p.name as cafe_name
            FROM {self.config.image_table} pi
            LEFT JOIN place p ON pi.place_id = p.place_id
            WHERE pi.place_image_id = %s AND pi.{self.config.path_column} IS NOT NULL
        """
        
        for attempt in range(self._max_retries):
            try:
                with self.get_connection() as connection:
                    cursor = connection.cursor(dictionary=True)
                    cursor.execute(query, (image_id,))
                    result = cursor.fetchone()
                    
                    if result:
                        self.logger.debug(f"Retrieved image record for ID {image_id}")
                    else:
                        self.logger.debug(f"No image record found for ID {image_id}")
                    
                    return result
                    
            except MySQLError as e:
                self.logger.warning(
                    f"Failed to retrieve image by ID {image_id} (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))
                else:
                    raise DatabaseError(
                        f"Failed to retrieve image by ID {image_id} after {self._max_retries} attempts: {e}"
                    )
    
    def get_images_count(self) -> int:
        """
        Get the total count of images in the database.
        
        Returns:
            Total number of image records
            
        Raises:
            DatabaseError: If query fails
        """
        query = f"""
            SELECT COUNT(*) as count
            FROM {self.config.image_table}
            WHERE {self.config.path_column} IS NOT NULL
        """
        
        for attempt in range(self._max_retries):
            try:
                with self.get_connection() as connection:
                    cursor = connection.cursor(dictionary=True)
                    cursor.execute(query)
                    result = cursor.fetchone()
                    
                    count = result['count'] if result else 0
                    self.logger.debug(f"Total image count: {count}")
                    return count
                    
            except MySQLError as e:
                self.logger.warning(
                    f"Failed to get image count (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))
                else:
                    raise DatabaseError(f"Failed to get image count after {self._max_retries} attempts: {e}")
    
    def batch_update_image_paths(self, updates: List[Tuple[int, str]]) -> Dict[str, Any]:
        """
        Update multiple image paths in a single transaction with rollback support.
        
        Args:
            updates: List of tuples (image_id, new_path) to update
            
        Returns:
            Dictionary with update results:
            - 'successful': Number of successful updates
            - 'failed': Number of failed updates
            - 'errors': List of error messages for failed updates
            
        Raises:
            DatabaseError: If transaction fails completely
        """
        if not updates:
            return {'successful': 0, 'failed': 0, 'errors': []}
        
        results = {'successful': 0, 'failed': 0, 'errors': []}
        
        for attempt in range(self._max_retries):
            connection = None
            try:
                with self.get_connection() as connection:
                    # Start transaction
                    connection.start_transaction()
                    cursor = connection.cursor()
                    
                    # Prepare batch update query
                    update_query = f"""
                        UPDATE {self.config.image_table}
                        SET {self.config.path_column} = %s
                        WHERE place_image_id = %s
                    """
                    
                    successful_updates = []
                    failed_updates = []
                    
                    # Process each update
                    for image_id, new_path in updates:
                        try:
                            # Verify record exists first
                            cursor.execute(f"SELECT place_image_id FROM {self.config.image_table} WHERE place_image_id = %s", (image_id,))
                            if not cursor.fetchone():
                                failed_updates.append((image_id, f"Record with ID {image_id} not found"))
                                continue
                            
                            # Perform update
                            cursor.execute(update_query, (new_path, image_id))
                            
                            if cursor.rowcount > 0:
                                successful_updates.append((image_id, new_path))
                            else:
                                failed_updates.append((image_id, f"Update affected 0 rows for ID {image_id}"))
                                
                        except MySQLError as e:
                            failed_updates.append((image_id, f"MySQL error: {str(e)}"))
                    
                    # If we have any successful updates, commit the transaction
                    if successful_updates:
                        connection.commit()
                        results['successful'] = len(successful_updates)
                        self.logger.info(f"Batch update committed: {len(successful_updates)} successful updates")
                        
                        # Log successful updates
                        for image_id, new_path in successful_updates:
                            self.logger.info(f"Updated image ID {image_id}: {new_path}")
                    else:
                        connection.rollback()
                        self.logger.warning("No successful updates in batch, transaction rolled back")
                    
                    # Record failed updates
                    results['failed'] = len(failed_updates)
                    results['errors'] = [f"ID {image_id}: {error}" for image_id, error in failed_updates]
                    
                    # Log failed updates
                    for image_id, error in failed_updates:
                        self.logger.warning(f"Failed to update image ID {image_id}: {error}")
                    
                    return results
                    
            except MySQLError as e:
                # Rollback transaction on error
                if connection:
                    try:
                        connection.rollback()
                        self.logger.info("Batch update transaction rolled back due to error")
                    except MySQLError as rollback_error:
                        self.logger.error(f"Failed to rollback batch transaction: {rollback_error}")
                
                error_code = getattr(e, 'errno', None)
                self.logger.warning(
                    f"Batch update failed (attempt {attempt + 1}/{self._max_retries}): "
                    f"Error {error_code}: {str(e)}"
                )
                
                if attempt < self._max_retries - 1:
                    wait_time = self._retry_delay * (2 ** attempt)
                    self.logger.info(f"Retrying batch update in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise DatabaseError(
                        f"Batch update failed after {self._max_retries} attempts. "
                        f"Last error (code {error_code}): {str(e)}"
                    )
            except Exception as e:
                if connection:
                    try:
                        connection.rollback()
                    except:
                        pass
                
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay)
                    continue
                else:
                    raise DatabaseError(f"Unexpected error in batch update: {str(e)}")
        
        return results
    
    def close(self) -> None:
        """Close all connections in the pool."""
        if self._connection_pool:
            try:
                # Force close all connections in the pool
                pool_name = self._connection_pool.pool_name
                
                # Try to close connections gracefully first
                closed_count = 0
                for _ in range(10):  # Try up to 10 connections
                    try:
                        connection = self._connection_pool.get_connection(timeout=1)
                        if connection.is_connected():
                            connection.close()
                        closed_count += 1
                    except:
                        break
                
                self.logger.info(f"Database connection pool '{pool_name}' closed ({closed_count} connections)")
                
            except Exception as e:
                self.logger.warning(f"Error closing connection pool: {e}")
            finally:
                self._connection_pool = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()