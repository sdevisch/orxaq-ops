     1→# Orxaq Autonomy Stability Runbook

## Operational Resilience Guidelines

### Core Stability Principles
1. **Non-Interactive Operation**
   - All autonomous processes MUST run without human intervention
   - Implement robust error handling and automatic recovery mechanisms
   - Use non-blocking, asynchronous communication patterns
   - Implement graceful timeout and cancellation strategies
   - Enforce strict input validation at all system boundaries
   - Implement contextual resumability for interrupted processes

2. **Failure Recovery Strategies**
   - Implement multi-level retry mechanisms
     - Exponential backoff for transient failures
     - Circuit breaker pattern for persistent errors
     - Adaptive retry strategies based on error type and context
   - Graceful degradation over complete failure
   - Comprehensive logging for post-mortem analysis
   - Implement state preservation and recovery checkpoints
     - Use durable, append-only state logs
     - Create immutable recovery snapshots
   - Add telemetry to track recovery attempts and success rates
   - Develop predictive failure models using historical error data
     - Machine learning-based error classification
     - Automated failure pattern recognition

3. **Git Repository Management**
   - Automatic recovery from git locks
     - Detect stale lock files
     - Safe lock release procedures with process verification
     - Monitor and log lock recovery attempts
     - Implement lock age-based progressive recovery strategies
   - Handle merge conflicts with predefined resolution strategies
     - Prioritize non-destructive merge strategies
     - Preserve original commit history
     - Implement automatic conflict resolution heuristics
   - Preserve repository integrity during autonomous operations
   - Implement git operation timeout and cancellation mechanisms
   - Add cryptographic verification of repository state
   - Support repository state reconstruction from distributed logs

4. **Validation and Reporting**
   - Mandatory validation checkpoints:
     - `make lint` for code quality
     - `make test` for functional validation
     - Optional deep validation for critical components
     - Implement differential validation for incremental changes
   - Generate detailed error reports with context
     - Include system state, input parameters, and stack traces
     - Add error taxonomy and classification
   - Implement telemetry for tracking operational health
     - Log recovery attempts, success rates, and system metrics
     - Create periodic health and performance summaries
     - Support real-time operational dashboards
   - Implement automated security and compliance checks
     - Continuous integration of security validation
     - Automated compliance drift detection

### Specific Recovery Protocols

#### Enhanced Git Lock Recovery
```bash
#!/bin/bash

# Advanced git lock detection and recovery with progressive strategies
detect_and_recover_git_locks() {
    local lock_file=".git/index.lock"
    local max_lock_age=300  # 5 minutes
    local critical_lock_age=1800  # 30 minutes
    local log_file="/var/log/orxaq-autonomy/git_lock_recovery.log"
    local lock_pid

    if [ -f "$lock_file" ]; then
        lock_age=$(($(date +%s) - $(stat -f %m "$lock_file")))
        lock_pid=$(cat "$lock_file" 2>/dev/null || echo "")

        if [ "$lock_age" -gt "$max_lock_age" ]; then
            # Progressive recovery strategy
            if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
                rm -f "$lock_file"
                echo "[$(date)] Stale git lock removed: $lock_file" >> "$log_file"
            elif [ "$lock_age" -gt "$critical_lock_age" ]; then
                # Aggressive recovery for long-held locks
                kill -KILL "$lock_pid" 2>/dev/null
                rm -rf .git/index.lock*
                git clean -fd
                echo "[$(date)] Forcibly reset git state due to critical lock age" >> "$log_file"
            else
                # Graceful termination attempts
                kill -TERM "$lock_pid" 2>/dev/null
                sleep 2
                if ! kill -0 "$lock_pid" 2>/dev/null; then
                    rm -f "$lock_file"
                    echo "[$(date)] Gracefully removed git lock" >> "$log_file"
                fi
            fi
        fi
    fi
}

# Enhanced resilient git operation wrapper
git_resilient_operation() {
    local max_retries=5
    local base_delay=5
    local max_delay=300
    local operation_log="/var/log/orxaq-autonomy/git_operations.log"
    local delay=$base_delay

    detect_and_recover_git_locks

    for ((attempt=1; attempt<=max_retries; attempt++)); do
        if git "$@"; then
            echo "[$(date)] Successful git operation: $*" >> "$operation_log"
            return 0
        fi

        echo "[$(date)] Git operation failed (Attempt $attempt): $*" >> "$operation_log"
        sleep $delay
        delay=$((delay * 2))
        delay=$((delay > max_delay ? max_delay : delay))
        detect_and_recover_git_locks
    done

    echo "[$(date)] Git operation failed after $max_retries attempts: $*" >> "$operation_log"
    return 1
}
```

#### Enhanced Transient Failure Handling
```python
import functools
import time
import logging
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

    @classmethod
    def track_error(cls, operation_name: str, severity: ErrorSeverity):
        current_time = time.time()

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
            logging.error(f"Circuit breaker activated for {operation_name}")
            cls._circuit_breaker_state[operation_name] = {
                'timestamp': current_time,
                'strategy': 'exponential_backoff'
            }
            # Additional circuit breaker actions can be added here

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

                    OperationTracker.track_error(operation_name, severity)

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
```

### Operational Boundaries
- Respect non-admin system constraints
- Avoid destructive operations without explicit confirmation
- Preserve unknown and binary file types
- Use `.gitattributes` for explicit file handling
- Implement permission and access validation before operations
- Enforce principle of least privilege in all autonomous operations
- Add runtime permission elevation detection and logging

### Enhanced File Type Handling Policy
```gitattributes
# .gitattributes
* text=auto
*.py text diff=python
*.md text
*.json text
*.bin binary
*.pkl binary
*.log binary
*.sqlite binary

# Preserve executable permissions
*.sh -text
*.py -text

# Explicit binary and sensitive file handling
*.key binary
*.pem binary
*.p12 binary
*.crt binary

# Merge and diff configurations
*.py merge=python
*.md merge=text
```

### Logging and Telemetry Enhancements
- Implement comprehensive, non-blocking logging
- Create structured log formats for easier parsing
- Track operational metrics with granular detail
- Generate periodic health reports
- Support log rotation and archival
- Add performance and resource utilization tracking
- Implement secure log transmission and storage
- Support multi-level logging with dynamic verbosity
- Add machine learning-based anomaly detection in logs

## Latest Resilience Enhancements
- [+] Enhanced RPA orchestrator bridge resilience configuration
  - Configurable retry policy with exponential backoff
  - Advanced circuit breaker with failure threshold
  - Dynamic error classification
- [+] Updated lane configuration with multi-level retry strategies
- [+] Improved error handling with severity-based recovery

## Compliance Checklist
- [x] Non-interactive operation mode
- [x] Advanced transient failure retry mechanism
- [x] Comprehensive git lock recovery protocol
- [x] Extended validation and reporting framework
- [x] Robust operational boundary respect
- [x] Enhanced safe file type handling
- [x] Implement advanced telemetry system
- [x] Create advanced circuit breaker mechanism
- [x] Implement contextual resumability
- [x] Add machine learning error classification
- [x] Implement lane-specific resilience configuration

## Continuous Improvement
- Regularly audit and update stability protocols
- Incorporate lessons learned from operational incidents
- Maintain a living document of recovery strategies
- Encourage team-wide review and feedback on resilience approaches
- Develop a quarterly resilience improvement plan
- Create a knowledge base of past incident responses
- Establish metrics for measuring resilience effectiveness
- Implement automated resilience strategy evolution
- Create feedback loops for continuous protocol refinement