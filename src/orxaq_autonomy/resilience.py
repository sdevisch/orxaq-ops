import functools
import time
import logging
import traceback
from typing import Callable, Any, Optional
from dataclasses import dataclass, field
from enum import Enum, auto

class ErrorSeverity(Enum):
    TRANSIENT = auto()
    TEMPORARY = auto()
    PERSISTENT = auto()

@dataclass
class ResilienceConfig:
    MAX_RETRIES: int = 5
    BASE_DELAY: float = 1.0
    MAX_DELAY: float = 300.0
    BACKOFF_FACTOR: float = 2.0
    ERROR_TRACKING_WINDOW: int = 60  # seconds
    ERROR_TRACKING_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN: int = 300  # seconds
    error_registry: dict = field(default_factory=dict)

class OperationTracker:
    _error_registry: dict = {}
    _circuit_breaker_state: dict = {}
    _logger = logging.getLogger('orxaq_autonomy.resilience')

    @classmethod
    def log_error(cls, operation_name: str, error: Exception, severity: ErrorSeverity):
        error_details = {
            'type': type(error).__name__,
            'message': str(error),
            'traceback': traceback.format_exc(),
            'severity': severity.name
        }
        cls._logger.error(f"Error in {operation_name}: {error_details}")

    @classmethod
    def track_error(cls, operation_name: str, error: Exception, severity: ErrorSeverity):
        current_time = time.time()
        cls.log_error(operation_name, error, severity)

        # Prune old error entries
        cls._error_registry[operation_name] = [
            entry for entry in cls._error_registry.get(operation_name, [])
            if current_time - entry['timestamp'] < ResilienceConfig.ERROR_TRACKING_WINDOW
        ]

        # Add new error
        cls._error_registry.setdefault(operation_name, []).append({
            'timestamp': current_time,
            'severity': severity
        })

        # Check for circuit breaker conditions
        error_count = len(cls._error_registry.get(operation_name, []))
        persistent_errors = sum(1 for entry in cls._error_registry.get(operation_name, [])
                                if entry['severity'] == ErrorSeverity.PERSISTENT)

        if error_count > ResilienceConfig.ERROR_TRACKING_THRESHOLD or persistent_errors > 0:
            cls.activate_circuit_breaker(operation_name)

    @classmethod
    def activate_circuit_breaker(cls, operation_name: str):
        current_time = time.time()
        last_break = cls._circuit_breaker_state.get(operation_name, {}).get('timestamp', 0)

        if current_time - last_break > ResilienceConfig.CIRCUIT_BREAKER_COOLDOWN:
            cls._logger.critical(f"Circuit breaker activated for {operation_name}")
            cls._circuit_breaker_state[operation_name] = {
                'timestamp': current_time,
                'strategy': 'exponential_backoff'
            }

    @classmethod
    def is_circuit_broken(cls, operation_name: str) -> bool:
        state = cls._circuit_breaker_state.get(operation_name)
        if not state:
            return False

        current_time = time.time()
        return current_time - state['timestamp'] < ResilienceConfig.CIRCUIT_BREAKER_COOLDOWN

def retry_with_advanced_backoff(
    max_retries: int = ResilienceConfig.MAX_RETRIES,
    base_delay: float = ResilienceConfig.BASE_DELAY,
    max_delay: float = ResilienceConfig.MAX_DELAY,
    backoff_factor: float = ResilienceConfig.BACKOFF_FACTOR,
    on_retry: Optional[Callable] = None
):
    """
    Advanced retry decorator with circuit breaker and dynamic error severity tracking.

    Args:
        max_retries (int): Maximum number of retry attempts
        base_delay (float): Initial delay between retries
        max_delay (float): Maximum delay between retries
        backoff_factor (float): Exponential backoff multiplier
        on_retry (Optional[Callable]): Optional callback for retry events
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            operation_name = func.__name__

            if OperationTracker.is_circuit_broken(operation_name):
                logging.warning(f"Circuit breaker active for {operation_name}. Skipping execution.")
                raise RuntimeError(f"Circuit breaker active for {operation_name}")

            delay = base_delay

            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    # Reset error tracking on successful execution
                    OperationTracker._error_registry[operation_name] = []
                    return result

                except Exception as e:
                    # Classify error severity dynamically
                    severity = (
                        ErrorSeverity.TRANSIENT
                        if isinstance(e, (ConnectionError, TimeoutError))
                        else ErrorSeverity.PERSISTENT
                    )

                    OperationTracker.track_error(operation_name, e, severity)

                    logging.warning(f"Attempt {attempt + 1} failed for {operation_name}: {e}")

                    if on_retry:
                        on_retry(attempt, e)

                    if attempt == max_retries - 1:
                        logging.error(f"Operation {operation_name} failed after {max_retries} attempts")
                        raise

                    time.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)

        return wrapper
    return decorator