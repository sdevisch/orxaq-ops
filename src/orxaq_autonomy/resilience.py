import functools
import time
import logging
import traceback
from typing import Callable, Any, Optional
from dataclasses import dataclass, field
from enum import Enum, auto

class ErrorSeverity(Enum):
    """
    Comprehensive error severity classification for advanced resilience.

    Levels:
    - TRANSIENT: Momentary, likely recoverable errors (network glitches)
    - TEMPORARY: Short-term errors that might resolve with retry
    - INTERMITTENT: Sporadic errors that suggest underlying instability
    - PERSISTENT: Consistent failures indicating systemic issues
    - CRITICAL: Errors that require immediate intervention
    """
    TRANSIENT = auto()      # Single occurrence, likely recoverable
    TEMPORARY = auto()      # Short-term, might need multiple retries
    INTERMITTENT = auto()   # Sporadic errors suggesting potential instability
    PERSISTENT = auto()     # Consistent failures indicating systemic problems
    CRITICAL = auto()       # Requires immediate manual intervention

@dataclass
class ResilienceConfig:
    """
    Advanced configuration for autonomous resilience and error recovery.

    Configurable parameters for retry, backoff, and circuit breaker strategies.
    """
    # Retry configuration
    MAX_RETRIES: int = 5
    BASE_DELAY: float = 1.0
    MAX_DELAY: float = 300.0
    BACKOFF_FACTOR: float = 2.0

    # Error tracking and circuit breaker parameters
    ERROR_TRACKING_WINDOW: int = 60  # seconds
    ERROR_TRACKING_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN: int = 300  # seconds

    # Severity-based retry configuration
    SEVERITY_RETRY_MULTIPLIERS: dict = field(default_factory=lambda: {
        ErrorSeverity.TRANSIENT: 1.0,
        ErrorSeverity.TEMPORARY: 1.5,
        ErrorSeverity.INTERMITTENT: 2.0,
        ErrorSeverity.PERSISTENT: 0.0,  # No retries
        ErrorSeverity.CRITICAL: 0.0     # No retries
    })

    # Logging configuration
    LOG_LEVEL: str = 'INFO'
    LOG_FORMAT: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

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
        """
        Advanced error tracking with comprehensive analysis and circuit breaker activation.

        Tracks errors, classifies severity, manages error registry, and activates circuit breakers.
        """
        current_time = time.time()
        cls.log_error(operation_name, error, severity)

        # Prune old error entries
        cls._error_registry[operation_name] = [
            entry for entry in cls._error_registry.get(operation_name, [])
            if current_time - entry['timestamp'] < ResilienceConfig.ERROR_TRACKING_WINDOW
        ]

        # Add new error with extended context
        error_entry = {
            'timestamp': current_time,
            'severity': severity,
            'type': type(error).__name__,
            'message': str(error)
        }
        cls._error_registry.setdefault(operation_name, []).append(error_entry)

        # Advanced error tracking and circuit breaker activation
        error_count = len(cls._error_registry.get(operation_name, []))
        severity_counts = {
            severity: sum(1 for entry in cls._error_registry.get(operation_name, [])
                          if entry['severity'] == severity)
            for severity in ErrorSeverity
        }

        # Complex circuit breaker conditions
        circuit_breaker_conditions = [
            error_count > ResilienceConfig.ERROR_TRACKING_THRESHOLD,
            severity_counts.get(ErrorSeverity.PERSISTENT, 0) > 0,
            severity_counts.get(ErrorSeverity.CRITICAL, 0) > 0,
            severity_counts.get(ErrorSeverity.INTERMITTENT, 0) > 2
        ]

        if any(circuit_breaker_conditions):
            cls.activate_circuit_breaker(operation_name, severity_counts)

    @classmethod
    def activate_circuit_breaker(cls, operation_name: str, severity_counts: dict = None):
        """
        Advanced circuit breaker activation with granular severity handling.

        Args:
            operation_name (str): Name of the operation experiencing repeated failures
            severity_counts (dict, optional): Breakdown of error severities
        """
        current_time = time.time()
        last_break = cls._circuit_breaker_state.get(operation_name, {}).get('timestamp', 0)

        # Adaptive cooldown based on severity
        severity_cooldown_multipliers = {
            ErrorSeverity.TRANSIENT: 1.0,
            ErrorSeverity.TEMPORARY: 1.5,
            ErrorSeverity.INTERMITTENT: 2.0,
            ErrorSeverity.PERSISTENT: 5.0,
            ErrorSeverity.CRITICAL: 10.0
        }

        # Determine the most severe encountered error
        if severity_counts:
            most_severe = max(
                (severity for severity in severity_counts.keys() if severity_counts.get(severity, 0) > 0),
                key=lambda s: list(ErrorSeverity).index(s)
            )
            cooldown_multiplier = severity_cooldown_multipliers.get(most_severe, 1.0)
        else:
            cooldown_multiplier = 1.0

        adaptive_cooldown = ResilienceConfig.CIRCUIT_BREAKER_COOLDOWN * cooldown_multiplier

        if current_time - last_break > adaptive_cooldown:
            cls._logger.critical(
                f"Circuit breaker activated for {operation_name}. "
                f"Severity profile: {severity_counts}. "
                f"Adaptive cooldown: {adaptive_cooldown} seconds."
            )
            cls._circuit_breaker_state[operation_name] = {
                'timestamp': current_time,
                'strategy': 'adaptive_backoff',
                'severity_profile': severity_counts,
                'cooldown_multiplier': cooldown_multiplier
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
    severity_multiplier_override: Optional[dict] = None,
    on_retry: Optional[Callable] = None,
    retry_policy_hint: Optional[str] = None
) -> Callable:
    """
    Advanced retry decorator with circuit breaker, dynamic error severity tracking, and adaptive retry policy.

    Args:
        max_retries (int): Maximum number of retry attempts
        base_delay (float): Initial delay between retries
        max_delay (float): Maximum delay between retries
        backoff_factor (float): Exponential backoff multiplier
        severity_multiplier_override (dict, optional): Custom severity retry multipliers
        on_retry (Optional[Callable]): Optional callback for retry events
        retry_policy_hint (str, optional): Contextual hint about retry strategy
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            operation_name = func.__name__

            if OperationTracker.is_circuit_broken(operation_name):
                logging.warning(
                    f"Circuit breaker active for {operation_name}. "
                    f"Skipping execution. Policy hint: {retry_policy_hint or 'N/A'}"
                )
                raise RuntimeError(f"Circuit breaker active for {operation_name}")

            delay = base_delay
            retry_multipliers = severity_multiplier_override or ResilienceConfig.SEVERITY_RETRY_MULTIPLIERS

            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    # Reset error tracking on successful execution
                    OperationTracker._error_registry[operation_name] = []
                    return result

                except Exception as e:
                    # Advanced error severity classification
                    if isinstance(e, ConnectionError):
                        severity = ErrorSeverity.TRANSIENT
                    elif isinstance(e, TimeoutError):
                        severity = ErrorSeverity.TEMPORARY
                    elif isinstance(e, ValueError) or isinstance(e, AttributeError):
                        severity = ErrorSeverity.INTERMITTENT
                    else:
                        severity = ErrorSeverity.PERSISTENT

                    OperationTracker.track_error(operation_name, e, severity)

                    # Severity-aware logging
                    logging.log(
                        {
                            ErrorSeverity.TRANSIENT: logging.WARNING,
                            ErrorSeverity.TEMPORARY: logging.WARNING,
                            ErrorSeverity.INTERMITTENT: logging.ERROR,
                            ErrorSeverity.PERSISTENT: logging.CRITICAL,
                            ErrorSeverity.CRITICAL: logging.CRITICAL
                        }.get(severity, logging.ERROR),
                        f"Attempt {attempt + 1} failed for {operation_name}: {e} [Severity: {severity.name}]"
                    )

                    retry_multiplier = retry_multipliers.get(severity, 1.0)
                    if retry_multiplier == 0:
                        logging.error(f"No retry for operation {operation_name} due to {severity.name} error")
                        raise

                    if on_retry:
                        on_retry(attempt, e, severity)

                    if attempt == max_retries - 1:
                        logging.error(f"Operation {operation_name} failed after {max_retries} attempts")
                        raise

                    time.sleep(delay * retry_multiplier)
                    delay = min(delay * backoff_factor, max_delay)

        return wrapper
    return decorator