import threading
import time
from typing import Optional, Any
import sys,os

class ThreadTemplate:
    """
    Template class demonstrating proper thread event handling and graceful exit.
    func_type : 1 Mean endless loop , 0 means functions, var, etc...
    """
    
    def __init__(self,target,func_type=1,*args, **kwargs):
        #super().__init__(*args, **kwargs)
        # Event to control thread execution
        self._stop_event = threading.Event()
        # Event to signal when thread is ready
        self._ready_event = threading.Event()
        # Thread instance
        self._thread: Optional[threading.Thread] = None
        # Lock for thread-safe operations
        self._lock = threading.Lock()
        # Flag to track if thread is running
        self._is_running = False
        self.main_function = target
        self.main_function_parameter = kwargs
        self.func_type = func_type
            
    def start(self) -> None:
        """
        Start the thread if it's not already running.
        """
        with self._lock:
            if not self._is_running:
                self._stop_event.clear()
                self._ready_event.clear()
                self._thread = threading.Thread(target=self._run)
                self._thread.start()
                # Wait for thread to be ready
                self._ready_event.wait(timeout=5.0)
                self._is_running = True
                print("Thread started successfully")
            else:
                print("Thread is already running")
    
    def stop(self) -> None:
        """
        Stop the thread gracefully.
        """
        with self._lock:
            if self._is_running:
                print("Stopping thread...")
                self._stop_event.set()
                if self._thread and self._thread.is_alive():
                    self._thread.join(timeout=0.05)
                self._is_running = False
                print("Thread stopped successfully")
            else:
                print("Thread is not running")
    
    def is_running(self) -> bool:
        """
        Check if the thread is currently running.
        
        Returns:
            bool: True if thread is running, False otherwise
        """
        return self._is_running
    
    def _run(self) -> None:
        """
        Main thread execution method.
        Override this method in your implementation.
        """
        try:
            # Signal that thread is ready
            self._ready_event.set()
            
            # Main execution loop
            if self.func_type == 1:
                while not self._stop_event.is_set():
                    # Your main processing logic goes here
                    # Example:
                    self.main_function(**self.main_function_parameter)
                    time.sleep(0.05)
            if self.func_type == 0:
                self.main_function(**self.main_function_parameter)
        except Exception as e:
            print(f"Error in thread execution: {str(e)}")
        finally:
            # Cleanup code goes here
            print("Thread cleanup completed")
    
    def __enter__(self):
        """
        Context manager entry.
        """
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit.
        """
        self.stop()

# Example usage:
if __name__ == "__main__":
    # Using context manager
    with ThreadTemplate() as thread:
        # Do something while thread is running
        time.sleep(3)
    
    # Or manual control
    thread = ThreadTemplate()
    thread.start()
    time.sleep(3)
    thread.stop() 