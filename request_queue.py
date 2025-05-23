import threading
import time
import uuid
import queue
import random
from typing import Dict, Any, Optional, Callable
from datetime import datetime, timedelta
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

class RequestQueue:
    def __init__(self, max_concurrent=1, cooldown_period=3):
        """
        Initialize the request queue system.
        
        Args:
            max_concurrent: Maximum number of concurrent requests allowed
            cooldown_period: Time in seconds to wait between batches of requests
        """
        self.queue = queue.Queue()
        self.results = {}
        self.statuses = {}  # Statuses: "queued", "processing", "completed", "failed"
        self.max_concurrent = max_concurrent
        self.cooldown_period = cooldown_period
        self.active_requests = 0
        self.lock = threading.Lock()
        self.last_request_time = None
        self.worker_thread = threading.Thread(target=self._process_queue)
        self.worker_thread.daemon = True
        self.worker_thread.start()
        logging.info(f"Request queue initialized with max_concurrent={max_concurrent}, cooldown_period={cooldown_period}s")
    
    def add_request(self, request_func, params):
        """
        Add a request to the queue.
        
        Args:
            request_func: Function to execute
            params: Parameters for the function
            
        Returns:
            request_id: A unique ID to track this request
        """
        request_id = str(uuid.uuid4())
        with self.lock:
            self.queue.put((request_id, request_func, params))
            self.statuses[request_id] = {
                "status": "queued",
                "position": self.queue.qsize(),
                "created_at": datetime.now(),
                "estimated_time": self._estimate_wait_time(self.queue.qsize())
            }
        return request_id
    
    def _estimate_wait_time(self, position):
        """Estimate wait time based on queue position"""
        # Rough estimate: each request takes about 5 seconds + cooldown
        avg_request_time = 5 + (self.cooldown_period / self.max_concurrent)
        batch = (position // self.max_concurrent)
        position_in_batch = position % self.max_concurrent
        
        if position_in_batch == 0:
            position_in_batch = self.max_concurrent
            batch -= 1
            
        wait_time = (batch * self.cooldown_period) + (position_in_batch * avg_request_time)
        return int(wait_time)
    
    def get_request_status(self, request_id):
        """
        Get the current status of a request
        
        Args:
            request_id: The ID of the request to check
            
        Returns:
            Dict with status information or None if not found
        """
        with self.lock:
            if request_id in self.statuses:
                status_data = self.statuses[request_id].copy()
                
                # Update position if still in queue
                if status_data["status"] == "queued":
                    position = 0
                    for i, (rid, _, _) in enumerate(list(self.queue.queue)):
                        if rid == request_id:
                            position = i + 1
                            break
                    status_data["position"] = position
                    status_data["estimated_time"] = self._estimate_wait_time(position)
                
                return status_data
            return None
    
    def get_request_result(self, request_id):
        """
        Get the result of a completed request
        
        Args:
            request_id: The ID of the request
            
        Returns:
            The result of the request if completed, None otherwise
        """
        with self.lock:
            if request_id in self.results:
                result = self.results[request_id]
                # Clean up
                del self.results[request_id]
                del self.statuses[request_id]
                return result
            return None
    
    def _process_queue(self):
        """Worker thread to process queued requests"""
        while True:
            # Process up to max_concurrent requests at a time
            batch = []
            with self.lock:
                # Check cooldown period
                if self.last_request_time and (datetime.now() - self.last_request_time) < timedelta(seconds=self.cooldown_period):
                    time_to_wait = (self.last_request_time + timedelta(seconds=self.cooldown_period) - datetime.now()).total_seconds()
                    if time_to_wait > 0:
                        logging.info(f"Waiting {time_to_wait:.2f} seconds for cooldown")
                        # Release lock while waiting
                        self.lock.release()
                        time.sleep(time_to_wait)
                        self.lock.acquire()
                
                # Get up to max_concurrent requests from the queue
                while len(batch) < self.max_concurrent and not self.queue.empty():
                    item = self.queue.get()
                    batch.append(item)
                    request_id = item[0]
                    self.statuses[request_id]["status"] = "processing"
                    logging.info(f"Request {request_id} status changed to processing")
                
                if batch:
                    logging.info(f"Processing batch of {len(batch)} requests")
                    self.last_request_time = datetime.now()
            
            # Process the batch
            for request_id, request_func, params in batch:
                try:
                    logging.info(f"Executing request {request_id}")
                    
                    # Add retry mechanism for 403 errors
                    max_retries = 3
                    retry_count = 0
                    retry_delay = 5  # seconds
                    
                    while retry_count < max_retries:
                        try:
                            result = request_func(**params)
                            # If successful, break out of retry loop
                            break
                        except Exception as e:
                            if "Rate limit exceeded" in str(e) or "403" in str(e):
                                retry_count += 1
                                if retry_count < max_retries:
                                    retry_delay_with_jitter = retry_delay + (retry_count * 2) + (random.random() * 2)
                                    logging.warning(f"Rate limit hit for request {request_id}, retrying in {retry_delay_with_jitter:.2f} seconds (attempt {retry_count}/{max_retries})")
                                    time.sleep(retry_delay_with_jitter)
                                    continue
                            # For non-rate limit errors or if max retries reached, re-raise
                            raise
                    
                    with self.lock:
                        self.results[request_id] = result
                        self.statuses[request_id]["status"] = "completed"
                        logging.info(f"Request {request_id} completed successfully")
                except Exception as e:
                    logging.exception(f"Error processing request {request_id}: {str(e)}")
                    with self.lock:
                        self.results[request_id] = {"error": str(e)}
                        self.statuses[request_id]["status"] = "failed"
            
            # If queue is empty, wait a bit before checking again
            if not batch:
                time.sleep(1)
                # Clean up old results and statuses
                self._cleanup_old_entries()
    
    def _cleanup_old_entries(self):
        """Clean up old completed/failed requests that haven't been retrieved"""
        with self.lock:
            current_time = datetime.now()
            expired_ids = []
            
            # Find expired entries (older than 30 minutes)
            for request_id, status in self.statuses.items():
                if status["status"] in ["completed", "failed"] and "created_at" in status:
                    time_diff = current_time - status["created_at"]
                    if time_diff.total_seconds() > 1800:  # 30 minutes
                        expired_ids.append(request_id)
            
            # Clean up expired entries
            for request_id in expired_ids:
                if request_id in self.results:
                    del self.results[request_id]
                if request_id in self.statuses:
                    del self.statuses[request_id]
                logging.info(f"Cleaned up expired request {request_id}")

# Global instance
request_queue = RequestQueue()