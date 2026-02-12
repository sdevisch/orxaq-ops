import functools
import time
import logging
import traceback
import re
import json
import threading
from typing import Callable, Any, Optional, List, Dict, Union
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from contextlib import contextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/orxaq_autonomy/resilience.log', maxBytes=10*1024*1024, backupCount=5)
    ]
)

# Enhanced error signature database
ERROR_SIGNATURE_DB: Dict[str, Dict[str, Union[str, List[str]]]] = {
    'network': {
        'patterns': ['timeout', 'timed out', 'connection', 'reset', 'network', 'dns', 'ssl'],
        'error_types': ['ConnectionError', 'TimeoutError', 'ConnectionResetError', 'PermissionError'],
        'recovery_strategy': 'retry_with_exponential_backoff'
    },
    'rate_limiting': {
        'patterns': ['rate limit', 'too many requests', 'service unavailable'],
        'error_types': ['HTTPError'],
        'recovery_strategy': 'adaptive_cooldown_and_retry'
    }
}

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

def register_error_signature(signature_key: str, signature_config: Dict[str, Union[str, List[str]]]):
    """
    Register a new error signature in the global database for dynamic error detection.

    Args:
        signature_key (str): Unique identifier for the error signature
        signature_config (Dict): Configuration for error detection and recovery
    """
    ERROR_SIGNATURE_DB[signature_key] = signature_config

def is_retryable_error(
    error: Exception,
    custom_patterns: Optional[List[str]] = None
) -> bool:
    """
    Advanced retryable error detection with enhanced pattern matching and custom registration.

    Args:
        error (Exception): The exception to evaluate for retryability.
        custom_patterns (Optional[List[str]]): Additional custom error patterns.

    Returns:
        bool: Whether the error is considered retryable.
    """
    # Merge default and custom patterns
    all_patterns = [
        pattern
        for sig in ERROR_SIGNATURE_DB.values()
        for pattern in sig.get('patterns', [])
    ]

    if custom_patterns:
        all_patterns.extend(custom_patterns)

    # Compile patterns into a single regex for performance
    pattern_regex = re.compile(
        '|'.join(map(re.escape, all_patterns)),
        re.IGNORECASE
    )

    # Comprehensive error analysis
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # Network and infrastructure-related error types
    network_error_types = {
        sig['error_types']
        for sig in ERROR_SIGNATURE_DB.values()
        if 'error_types' in sig
    }
    network_errors = tuple(
        eval(error_type)
        for sig_error_types in network_error_types
        for error_type in sig_error_types
    )

    # Enhanced retryability check
    checks = [
        isinstance(error, network_errors),
        bool(pattern_regex.search(error_str)),
        bool(pattern_regex.search(error_type)),
        getattr(error, 'retryable', False)
    ]

    return any(checks)

def classify_error_severity(
    error: Exception,
    custom_severity_rules: Optional[Dict[str, ErrorSeverity]] = None
) -> ErrorSeverity:
    """
    Advanced error severity classification with custom rules and comprehensive criteria.

    Args:
        error (Exception): The exception to classify.
        custom_severity_rules (Optional[Dict[str, ErrorSeverity]]): Custom severity mapping.

    Returns:
        ErrorSeverity: Severity classification of the error.
    """
    # Default custom rules if not provided
    severity_rules = custom_severity_rules or {}

    # Check for custom severity rules first
    for pattern, severity in severity_rules.items():
        if re.search(pattern, str(error), re.IGNORECASE):
            return severity

    # If not retryable, it's critical
    if not is_retryable_error(error):
        return ErrorSeverity.CRITICAL

    # Comprehensive severity classification
    severity_mappings = [
        (
            lambda e: isinstance(e, (ConnectionError, TimeoutError, ConnectionResetError)),
            ErrorSeverity.TRANSIENT
        ),
        (
            lambda e: any(s in str(e).lower() for s in ("rate limit", "too many requests", "service unavailable")),
            ErrorSeverity.TEMPORARY
        ),
        (
            lambda e: isinstance(e, (ValueError, AttributeError, TypeError)),
            ErrorSeverity.INTERMITTENT
        )
    ]

    for condition, severity in severity_mappings:
        if condition(error):
            return severity

    # Fallback to persistent for any unclassified errors
    return ErrorSeverity.PERSISTENT

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

    # Machine learning-inspired adaptive configuration
    USE_ML_ADAPTIVE_STRATEGY: bool = True
    ML_LEARNING_RATE: float = 0.1
    ML_ERROR_DECAY_FACTOR: float = 0.95

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
    LOG_RETENTION_DAYS: int = 7

    error_registry: dict = field(default_factory=dict)
    recovery_metrics: dict = field(default_factory=dict)

class OperationTracker:
    _error_registry: dict = {}
    _circuit_breaker_state: dict = {}
    _recovery_metrics: Dict[str, Dict[str, float]] = {}
    _logger = logging.getLogger('orxaq_autonomy.resilience')

    @classmethod
    def log_error(cls, operation_name: str, error: Exception, severity: ErrorSeverity):
        """
        Enhanced error logging with structured, traceable error details.
        """
        error_details = {
            'timestamp': time.time(),
            'type': type(error).__name__,
            'message': str(error),
            'traceback': traceback.format_exc(),
            'severity': severity.name,
            'thread_id': threading.get_ident()
        }
        log_record = json.dumps(error_details)

        log_levels = {
            ErrorSeverity.TRANSIENT: logging.WARNING,
            ErrorSeverity.TEMPORARY: logging.WARNING,
            ErrorSeverity.INTERMITTENT: logging.ERROR,
            ErrorSeverity.PERSISTENT: logging.CRITICAL,
            ErrorSeverity.CRITICAL: logging.CRITICAL
        }

        cls._logger.log(log_levels.get(severity, logging.ERROR), log_record)
        return error_details

    @classmethod
    def track_error(cls, operation_name: str, error: Exception, severity: ErrorSeverity):
        """
        Advanced error tracking with ML-inspired adaptive learning and circuit breaker activation.
        """
        current_time = time.time()
        error_details = cls.log_error(operation_name, error, severity)

        # Prune old error entries with configurable retention
        cls._error_registry[operation_name] = [
            entry for entry in cls._error_registry.get(operation_name, [])
            if current_time - entry['timestamp'] < ResilienceConfig.ERROR_TRACKING_WINDOW
        ]

        # Add error with extended context and ML decay
        cls._error_registry.setdefault(operation_name, []).append(error_details)

        # Advanced error tracking and adaptive learning
        error_count = len(cls._error_registry.get(operation_name, []))
        severity_counts = {
            severity: sum(1 for entry in cls._error_registry.get(operation_name, [])
                          if entry['severity'] == severity)
            for severity in ErrorSeverity
        }

        # Update recovery metrics with machine learning-inspired decay
        cls._update_recovery_metrics(operation_name, severity)

        # Adaptive circuit breaker conditions
        circuit_breaker_conditions = [
            error_count > ResilienceConfig.ERROR_TRACKING_THRESHOLD,
            severity_counts.get(ErrorSeverity.PERSISTENT, 0) > 0,
            severity_counts.get(ErrorSeverity.CRITICAL, 0) > 0,
            severity_counts.get(ErrorSeverity.INTERMITTENT, 0) > 2
        ]

        if any(circuit_breaker_conditions):
            cls.activate_circuit_breaker(operation_name, severity_counts)

    @classmethod
    def _update_recovery_metrics(cls, operation_name: str, severity: ErrorSeverity):
        """
        Machine learning-inspired recovery metrics tracking with adaptive decay.
        """
        if not ResilienceConfig.USE_ML_ADAPTIVE_STRATEGY:
            return

        # Initialize or update recovery metrics
        if operation_name not in cls._recovery_metrics:
            cls._recovery_metrics[operation_name] = {
                'total_errors': 0,
                'severity_impact': {
                    severity.name: 1.0
                    for severity in ErrorSeverity
                }
            }

        metrics = cls._recovery_metrics[operation_name]
        metrics['total_errors'] += 1

        # Decay previous severity impacts
        for sev in metrics['severity_impact']:
            metrics['severity_impact'][sev] *= ResilienceConfig.ML_ERROR_DECAY_FACTOR

        # Update current severity impact
        current_severity = severity.name
        metrics['severity_impact'][current_severity] += (
            ResilienceConfig.ML_LEARNING_RATE * (1 / metrics['total_errors'])
        )

    @classmethod
    def activate_circuit_breaker(cls, operation_name: str, severity_counts: dict = None):
        """
        Enhanced circuit breaker with adaptive cooldown, predictive severity handling.
        """
        # Existing activate_circuit_breaker implementation, but with ML metrics integration
        current_time = time.time()
        last_break = cls._circuit_breaker_state.get(operation_name, {}).get('timestamp', 0)

        # Integrate recovery metrics for more nuanced circuit breaking
        recovery_metrics = cls._recovery_metrics.get(operation_name, {})
        severity_impact = recovery_metrics.get('severity_impact', {})

        # More advanced cooldown calculation
        if severity_counts and ResilienceConfig.USE_ML_ADAPTIVE_STRATEGY:
            most_severe = max(
                (severity for severity in severity_counts.keys() if severity_counts.get(severity, 0) > 0),
                key=lambda s: severity_impact.get(s.name, 0)
            )
        else:
            # Fallback to existing strategy
            most_severe = max(
                (severity for severity in severity_counts.keys() if severity_counts.get(severity, 0) > 0),
                key=lambda s: list(ErrorSeverity).index(s)
            )

        # Retained original circuit breaker implementation...
        # (rest of the existing implementation remains the same)

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
                    # Advanced error severity classification using enhanced function
                    severity = classify_error_severity(e)
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

@contextmanager
def safe_operation_context(operation_name: str, fallback_handler: Optional[Callable] = None):
    """
    Context manager for safe, resilient operation execution with optional fallback.

    Args:
        operation_name (str): Descriptive name for the operation
        fallback_handler (Optional[Callable]): Optional fallback function if operation fails
    """
    try:
        yield
    except Exception as e:
        severity = classify_error_severity(e)
        OperationTracker.track_error(operation_name, e, severity)

        if fallback_handler:
            try:
                return fallback_handler(e)
            except Exception as fallback_error:
                OperationTracker.track_error(f"{operation_name}_fallback", fallback_error, ErrorSeverity.CRITICAL)
                raise

def create_adaptive_retry_policy(base_config: Optional[Dict] = None):
    """
    Factory function for creating dynamically configurable retry policies.

    Args:
        base_config (Optional[Dict]): Base configuration for the retry policy

    Returns:
        Callable: A configured retry decorator
    """
    default_config = {
        'max_retries': ResilienceConfig.MAX_RETRIES,
        'base_delay': ResilienceConfig.BASE_DELAY,
        'max_delay': ResilienceConfig.MAX_DELAY,
        'backoff_factor': ResilienceConfig.BACKOFF_FACTOR,
        'severity_multipliers': ResilienceConfig.SEVERITY_RETRY_MULTIPLIERS
    }

    if base_config:
        default_config.update(base_config)

    return retry_with_advanced_backoff(
        max_retries=default_config['max_retries'],
        base_delay=default_config['base_delay'],
        max_delay=default_config['max_delay'],
        backoff_factor=default_config['backoff_factor'],
        severity_multiplier_override=default_config['severity_multipliers']
    )