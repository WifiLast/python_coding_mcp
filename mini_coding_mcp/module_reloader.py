import importlib
import sys
from typing import Dict, List, Optional, Type, Any
#import numpy

import importlib.util
import threading
import ctypes
import time
import shutil
import os
import tempfile
from datetime import datetime
import hashlib

try:
    from . import module_thread_template
except ImportError:
    try:
        import module_thread_template
    except ImportError:
        module_thread_template = None


class ModuleReloader:
    """
    A class that manages dynamic reloading of Python modules during runtime.
    """
    
    def __init__(self):
        self._loaded_modules: Dict[str, Any] = {}
        self._module_dependencies: Dict[str, List[str]] = {}
        self._module_threads = {}
        self._thread_configs = {}  # Store thread configurations
        self.module_path = {}

        # Use temp directory for backups (writable and accessible)
        self.backup_dir = os.path.join(tempfile.gettempdir(), "mcp_module_backups")

        # Create backup directory if it doesn't exist, with graceful fallback
        try:
            if not os.path.exists(self.backup_dir):
                os.makedirs(self.backup_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            # If we can't create the backup dir, disable backups
            print(f"Warning: Could not create backup directory {self.backup_dir}: {e}")
            print("Module backups will be disabled")
            self.backup_dir = None
    
    def _calculate_file_hash(self, file_path: str) -> str:
        """
        Calculate SHA-256 hash of a file.
        
        Args:
            file_path (str): Path to the file to hash
            
        Returns:
            str: SHA-256 hash of the file
        """
        try:
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            print(f"Error calculating hash for {file_path}: {str(e)}")
            return ""

    def _create_backup(self, module_name: str, source_path: str) -> str:
        """
        Create a backup of a module file if it's different from existing backups.

        Args:
            module_name (str): Name of the module
            source_path (str): Path to the source file

        Returns:
            str: Path to the backup file
        """
        # Skip backups if backup_dir is not available
        if not self.backup_dir:
            return ""

        try:
            # Calculate hash of source file
            source_hash = self._calculate_file_hash(source_path)
            if not source_hash:
                return ""

            # Get all backup files for this module
            backup_files = [f for f in os.listdir(self.backup_dir)
                          if f.startswith(f"{module_name}_") and f.endswith(".py")]

            # Check if a backup with the same hash exists
            for backup_file in backup_files:
                backup_path = os.path.join(self.backup_dir, backup_file)
                backup_hash = self._calculate_file_hash(backup_path)
                if backup_hash == source_hash:
                    print(f"Backup already exists with same hash for {module_name}")
                    return backup_path

            # Create new backup if no matching hash found
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"{module_name}_{timestamp}.py"
            backup_path = os.path.join(self.backup_dir, backup_filename)

            # Create backup
            shutil.copy2(source_path, backup_path)
            print(f"Created new backup of {module_name} at {backup_path}")
            return backup_path

        except Exception as e:
            print(f"Error creating backup for {module_name}: {str(e)}")
            return ""
    
    def _load_from_backup(self, module_name: str) -> Optional[Any]:
        """
        Attempt to load a module from its most recent backup.

        Args:
            module_name (str): Name of the module to load from backup

        Returns:
            Optional[Any]: The loaded module if successful, None otherwise
        """
        # Skip backup loading if backup_dir is not available
        if not self.backup_dir:
            return None

        try:
            # Get all backup files for this module
            backup_files = [f for f in os.listdir(self.backup_dir)
                          if f.startswith(f"{module_name}_") and f.endswith(".py")]
            if not backup_files:
                print(f"No backup files found for {module_name}")
                return None

            # Sort by timestamp (newest first) and get the most recent backup
            backup_files.sort(reverse=True)
            latest_backup = backup_files[0]
            backup_path = os.path.join(self.backup_dir, latest_backup)

            print(f"Attempting to load {module_name} from backup: {latest_backup}")

            # Load the backup file
            spec = importlib.util.spec_from_file_location(module_name, backup_path)
            if not spec or not spec.loader:
                print(f"Failed to create spec for backup file: {backup_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        except Exception as e:
            print(f"Error loading module from backup: {str(e)}")
            return None

    def _create_cython_version(self, module_name: str, source_path: str) -> bool:
        """
        Create a Cython version of the module file.
        
        Args:
            module_name (str): Name of the module
            source_path (str): Path to the source file
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create .pyx copy
            pyx_path = source_path.replace('.py', '.pyx')
            shutil.copy2(source_path, pyx_path)
            print(f"Created Cython source file at {pyx_path}")
            
            # Run cythonize command
            import subprocess
            try:
                result = subprocess.run(['cythonize', '-a', '-i', pyx_path], 
                                 capture_output=True, 
                                 text=True)
            except:
                return False
            if result.returncode == 0:
                print(f"Successfully Cythonized {module_name}")
                
                # Clean up generated files
                base_path = os.path.splitext(pyx_path)[0]
                files_to_remove = [
                    f"{base_path}.c",      # C source file
                    f"{base_path}.html",   # HTML annotation file
                    pyx_path              # .pyx source file
                ]
                
                for file in files_to_remove:
                    try:
                        if os.path.exists(file):
                            os.remove(file)
                            print(f"Cleaned up {file}")
                    except Exception as e:
                        print(f"Warning: Could not remove {file}: {str(e)}")
                
                return True
            else:
                print(f"Error Cythonizing {module_name}: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"Error creating Cython version: {str(e)}")
            return False

    def register_module(self, module_name: str, custom_path: Optional[str] = None, use_cython: bool = False) -> None:
        """
        Register a module for reloading.
        
        Args:
            module_name (str): The name of the module to register (e.g., 'my_module')
            custom_path (Optional[str]): Custom path to add to sys.path for module import
            use_cython (bool): Whether to create and use a Cython version of the module
        """
        # If the module is already in sys.modules, track it but don't reload
        if module_name in sys.modules:
            module = sys.modules[module_name]
            if module_name not in self._loaded_modules:
                self._loaded_modules[module_name] = module
                self._module_dependencies[module_name] = self._get_module_dependencies(module)
                if hasattr(module, "__spec__") and module.__spec__ and module.__spec__.origin:
                    self.module_path.setdefault(module_name, module.__spec__.origin)
            print(f"Module {module_name} already in sys.modules, tracked by reloader")
            return
        else:
            if custom_path and custom_path not in sys.path:
                self.module_path[module_name] = custom_path
                sys.path.append(custom_path)
                
                try:
                    spec = importlib.util.spec_from_file_location(module_name, custom_path)
                    foo = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = foo
                    spec.loader.exec_module(foo)
                    
                    # Create backup only after successful import
                    if os.path.exists(custom_path):
                        self._create_backup(module_name, custom_path)
                        
                        # Create Cython version if requested
                        if use_cython:
                            if self._create_cython_version(module_name, custom_path):
                                # Try to import the Cython version
                                try:
                                    # first remove "old" module befor loading the new one.
                                    del sys.modules[module_name]
                                    cython_module = importlib.import_module(module_name)
                                    sys.modules[module_name] = cython_module
                                    foo = cython_module
                                    print(f"Using Cython version of {module_name}")
                                except ImportError as e:
                                    print(f"Failed to import Cython version: {str(e)}")
                                    print("Falling back to Python version")
                        
                except Exception as e:
                    print(f"Error loading module from custom path: {str(e)}")
                    print("Attempting to load from backup...")
                    backup_module = self._load_from_backup(module_name)
                    if backup_module:
                        sys.modules[module_name] = backup_module
                        foo = backup_module
                    else:
                        print(f"Failed to load module {module_name} from both source and backup")
                        return
                
            if module_name not in self._loaded_modules:
                try:
                    # Import the module
                    module = importlib.import_module(module_name)
                    self._loaded_modules[module_name] = module
                    self._module_dependencies[module_name] = self._get_module_dependencies(module)
                    # Make module globally available if not already in sys.modules
                    if module_name not in sys.modules:
                        sys.modules[module_name] = module
                    # Store file path so reload_module can reload without a custom_path
                    if hasattr(module, "__spec__") and module.__spec__ and module.__spec__.origin:
                        self.module_path.setdefault(module_name, module.__spec__.origin)
                    print(f"Successfully registered module: {module_name}")
                except ImportError as e:
                    print(f"Error registering module {module_name}: {str(e)}")
                    print("Attempting to load from backup...")
                    backup_module = self._load_from_backup(module_name)
                    if backup_module:
                        self._loaded_modules[module_name] = backup_module
                        self._module_dependencies[module_name] = self._get_module_dependencies(backup_module)
                        if module_name not in sys.modules:
                            sys.modules[module_name] = backup_module
                        print(f"Successfully loaded module {module_name} from backup")
                    else:
                        print(f"Failed to load module {module_name} from both source and backup")
    
    def reload_module(self, module_name: str) -> None:
        """
        Reload a registered module and its dependencies.
        
        Args:
            module_name (str): The name of the module to reload
        """
        if module_name not in self._loaded_modules:
            print(f"Module {module_name} is not registered")
            return
            
        #try:
        # Store thread configuration if it exists
        thread_config = self._thread_configs.get(module_name)
        
        # Kill any existing threads for this module
        if module_name in self._module_threads:
            print(f"Killing existing threads for module: {module_name}")
            self.kill_thread(module_name)
        
        # Reload the module — prefer importlib.reload for package modules to preserve
        # relative-import context; fall back to spec_from_file_location for file-path loads.
        file_path = self.module_path.get(module_name)
        if module_name in sys.modules and "." in module_name:
            try:
                foo = importlib.reload(sys.modules[module_name])
            except Exception as exc:
                print(f"importlib.reload failed for {module_name}: {exc}; falling back to spec load")
                if not file_path:
                    raise
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                foo = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = foo
                spec.loader.exec_module(foo)
        else:
            if not file_path:
                print(f"No file path available to reload {module_name}")
                return
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            foo = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = foo
            spec.loader.exec_module(foo)
        self._loaded_modules[module_name] = foo
        
        # Update dependencies
        self._module_dependencies[module_name] = self._get_module_dependencies(foo)
        
        # Reload dependent modules
        for dep_module in self._module_dependencies[module_name]:
            if dep_module in self._loaded_modules:
                self.reload_module(dep_module)
                
        print(f"Successfully reloaded module: {module_name}")
        
        # Restart thread if it was running before
        if thread_config:
            print(f"Restarting thread for module: {module_name}")
            self.start_module_thread(
                module_name=module_name,
                target_function=thread_config['target_function'],
                *thread_config['args'],
                **thread_config['kwargs']
            )
            
        #except Exception as e:
        #    print(f"Error reloading module {module_name}: {str(e)}")
    
    def reload_all(self) -> None:
        """Reload all registered modules."""
        for module_name in list(self._loaded_modules.keys()):
            self.reload_module(module_name)
    
    def get_module(self, module_name: str) -> Optional[Any]:
        """
        Get a registered module instance.
        
        Args:
            module_name (str): The name of the module to retrieve
            
        Returns:
            Optional[Any]: The module instance if found, None otherwise
        """
        return self._loaded_modules.get(module_name)
    
    def _get_module_dependencies(self, module: Any) -> List[str]:
        """
        Get the dependencies of a module by analyzing its imports.
        
        Args:
            module (Any): The module to analyze
            
        Returns:
            List[str]: List of module names that this module depends on
        """
        dependencies = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type):
                # Get the module name of the class
                module_name = attr.__module__
                if module_name and module_name != module.__name__:
                    dependencies.append(module_name)
        return list(set(dependencies))
    
    def get_loaded_modules(self) -> List[str]:
        """
        Get the list of all registered module names.
        
        Returns:
            List[str]: List of module names that are currently registered
        """
        return list(self._loaded_modules.keys())

    def get_module_by_name(self, module_name: str) -> Optional[Any]:
        """
        Get a module by its name from sys.modules.
        
        Args:
            module_name (str): The name of the module to retrieve
            
        Returns:
            Optional[Any]: The module instance if found, None otherwise
        """
        return sys.modules.get(module_name)

    def start_module_thread(self, module_name: str, target_function: str, *args, **kwargs) -> None:
        """
        Start a module's function in a new thread.
        
        Args:
            module_name (str): The name of the module to run
            target_function (str): The name of the function to run in the thread
            *args: Variable length argument list to pass to the target function
            **kwargs: Arbitrary keyword arguments to pass to the target function
        """
        if module_name not in self._loaded_modules:
            print(f"Module {module_name} is not registered")
            return

        if module_thread_template is None:
            print("module_thread_template is not available; cannot start thread")
            return

        try:
            module = self._loaded_modules[module_name]
            target_func = getattr(module, target_function)

            thread = module_thread_template.ThreadTemplate(target=target_func,*args,**kwargs)
            thread.start()
            self._module_threads[module_name] = thread
            
            # Store thread configuration
            self._thread_configs[module_name] = {
                'target_function': target_function,
                'args': args,
                'kwargs': kwargs
            }
            
            print(f"Started {target_function} from {module_name} in new thread")
        except AttributeError:
            print(f"Function {target_function} not found in module {module_name}")
        except Exception as e:
            print(f"Error starting thread for {module_name}: {str(e)}")
    
    def kill_thread(self, module_name: str) -> None:
        """
        Kill a running thread for a specific module.
        
        Args:
            module_name (str): The name of the module whose thread should be killed
        """
        if module_name in self._module_threads:
            thread = self._module_threads[module_name]
            thread.stop()
            #thread.join(timeout=1.0)  # Wait up to 1 second for the thread to finish
            del self._module_threads[module_name]
            print(f"Killed thread for module: {module_name}")
        else:
            print(f"No thread found for module: {module_name}")

    def load_module_function(self, module_name: str, function_name: str, *args, **kwargs) -> Any:
        """
        Load and execute a function from a module.
        
        Args:
            module_name (str): The name of the module containing the function
            function_name (str): The name of the function to execute
            *args: Variable length argument list to pass to the function
            **kwargs: Arbitrary keyword arguments to pass to the function
            
        Returns:
            Any: The result of the function execution if successful, None otherwise
        """
        try:
            # First try to get the module from loaded modules
            module = self._loaded_modules.get(module_name)
            if not module:
                # If not loaded, try to get from sys.modules
                module = sys.modules.get(module_name)
                if not module:
                    # If still not found, try to import it
                    module = importlib.import_module(module_name)
                    self._loaded_modules[module_name] = module

            # Get the function from the module
            target_func = getattr(module, function_name)
            if not callable(target_func):
                print(f"Error: {function_name} in module {module_name} is not callable")
                return None

            # Execute the function
            return target_func(*args, **kwargs)

        except ImportError as e:
            print(f"Error importing module {module_name}: {str(e)}")
            return None
        except AttributeError as e:
            print(f"Error accessing function {function_name} in module {module_name}: {str(e)}")
            return None
        except Exception as e:
            print(f"Error executing function {function_name} from module {module_name}: {str(e)}")
            return None

# Example usage:
if __name__ == "__main__":
    # Create a reloader instance
    reloader = ModuleReloader()
    
    # Register a module
    # python_module.import_test1
    #reloader.register_module("numpy")
    #reloader.register_module("test1", custom_path="path/to/module.py")
    
    # Example 1: Load and execute a function from numpy
    #print("\nExample 1: Using numpy function")
    #result = reloader.load_module_function("numpy", "array", [1, 2, 3])
    #print(f"Numpy array result: {result}")
    
    # Example 2: Load and execute a function from test1 module
    reloader.register_module("test1", custom_path="import_test1.py",use_cython=True)
    print("\nExample 1: Using test1 function")
    result = reloader.load_module_function("test1", "test1", a="test parameter")
    print(f"Test1 function result: {result}")
    
    reloader.register_module("test1", custom_path="import_test2.py",use_cython=True)
    print("\nExample 2: Using test2 function")
    result = reloader.load_module_function("test1", "test1", a="test parameter")
    print(f"Test2 function result: {result}")    
    
