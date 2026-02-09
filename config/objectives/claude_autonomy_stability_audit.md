# Orxaq Autonomy Stability Runbook

## Operational Resilience Guidelines

### Core Stability Principles
1. **Non-Interactive Operation**
   - All autonomous processes MUST run without human intervention
   - Implement robust error handling and automatic recovery mechanisms
   - Use non-blocking, asynchronous communication patterns
   - Implement graceful timeout and cancellation strategies

2. **Failure Recovery Strategies**
   - Implement multi-level retry mechanisms
     - Exponential backoff for transient failures
     - Circuit breaker pattern for persistent errors
   - Graceful degradation over complete failure
   - Comprehensive logging for post-mortem analysis
   - Implement state preservation and recovery checkpoints
   - Add telemetry to track recovery attempts and success rates

3. **Git Repository Management**
   - Automatic recovery from git locks
     - Detect stale lock files
     - Safe lock release procedures with process verification
     - Monitor and log lock recovery attempts
   - Handle merge conflicts with predefined resolution strategies
     - Prioritize non-destructive merge strategies
     - Preserve original commit history
   - Preserve repository integrity during autonomous operations
   - Implement git operation timeout and cancellation mechanisms

4. **Validation and Reporting**
   - Mandatory validation checkpoints:
     - `make lint` for code quality
     - `make test` for functional validation
     - Optional deep validation for critical components
   - Generate detailed error reports with context
     - Include system state, input parameters, and stack traces
   - Implement telemetry for tracking operational health
     - Log recovery attempts, success rates, and system metrics
     - Create periodic health and performance summaries

### Specific Recovery Protocols

#### Enhanced Git Lock Recovery
```bash
#!/bin/bash

# Advanced git lock detection and recovery
detect_and_recover_git_locks() {
    local lock_file=".git/index.lock"
    local max_lock_age=300  # 5 minutes
    local log_file="/var/log/orxaq-autonomy/git_lock_recovery.log"

    if [ -f "$lock_file" ]; then
        # Get lock file age and PID
        lock_age=$(($(date +%s) - $(stat -f %m "$lock_file")))
        lock_pid=$(cat "$lock_file")

        # Check if lock is stale (> 5 minutes)
        if [ "$lock_age" -gt "$max_lock_age" ]; then
            # Verify if process is actually dead
            if ! kill -0 "$lock_pid" 2>/dev/null; then
                rm -f "$lock_file"
                echo "[$(date)] Stale git lock removed: $lock_file" >> "$log_file"
            else
                # Attempt to gracefully terminate the process
                kill "$lock_pid"
                sleep 2
                kill -9 "$lock_pid" 2>/dev/null
                rm -f "$lock_file"
                echo "[$(date)] Forcibly removed git lock: $lock_file" >> "$log_file"
            fi
        fi
    fi
}

# Resilient git operation wrapper
git_resilient_operation() {
    local max_retries=3
    local retry_delay=5
    local operation_log="/var/log/orxaq-autonomy/git_operations.log"

    detect_and_recover_git_locks

    for ((attempt=1; attempt<=max_retries; attempt++)); do
        if git "$@"; then
            echo "[$(date)] Successful git operation: $*" >> "$operation_log"
            return 0
        fi

        echo "[$(date)] Git operation failed (Attempt $attempt): $*" >> "$operation_log"
        sleep $retry_delay
        retry_delay=$((retry_delay * 2))
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

class ResilienceConfig:
    MAX_RETRIES: int = 3
    BASE_DELAY: float = 1.0
    BACKOFF_FACTOR: float = 2.0
    ERROR_TRACKING_THRESHOLD: int = 5

class OperationTracker:
    _error_counts: dict = {}

    @classmethod
    def track_error(cls, operation_name: str):
        cls._error_counts[operation_name] = cls._error_counts.get(operation_name, 0) + 1

        if cls._error_counts[operation_name] > ResilienceConfig.ERROR_TRACKING_THRESHOLD:
            logging.error(f"High error rate detected for {operation_name}")
            # Potential circuit breaker or alert mechanism

def retry_with_advanced_backoff(
    max_retries: int = ResilienceConfig.MAX_RETRIES,
    base_delay: float = ResilienceConfig.BASE_DELAY,
    backoff_factor: float = ResilienceConfig.BACKOFF_FACTOR,
    on_retry: Optional[Callable] = None
):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            operation_name = func.__name__
            delay = base_delay

            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    # Reset error count on successful execution
                    OperationTracker._error_counts[operation_name] = 0
                    return result

                except Exception as e:
                    OperationTracker.track_error(operation_name)
                    logging.warning(f"Attempt {attempt + 1} failed for {operation_name}: {e}")

                    if on_retry:
                        on_retry(attempt, e)

                    if attempt == max_retries - 1:
                        logging.error(f"Operation {operation_name} failed after {max_retries} attempts")
                        raise

                    time.sleep(delay)
                    delay *= backoff_factor

        return wrapper
    return decorator
```

### Operational Boundaries
- Respect non-admin system constraints
- Avoid destructive operations without explicit confirmation
- Preserve unknown and binary file types
- Use `.gitattributes` for explicit file handling
- Implement permission and access validation before operations

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
```

### Logging and Telemetry Enhancements
- Implement comprehensive, non-blocking logging
- Create structured log formats for easier parsing
- Track operational metrics with granular detail
- Generate periodic health reports
- Support log rotation and archival
- Add performance and resource utilization tracking

## Compliance Checklist
- [x] Non-interactive operation mode
- [x] Advanced transient failure retry mechanism
- [x] Comprehensive git lock recovery protocol
- [x] Extended validation and reporting framework
- [x] Robust operational boundary respect
- [x] Enhanced safe file type handling
- [ ] Implement advanced telemetry system
- [ ] Create circuit breaker mechanism

## Continuous Improvement
- Regularly audit and update stability protocols
- Incorporate lessons learned from operational incidents
- Maintain a living document of recovery strategies
- Encourage team-wide review and feedback on resilience approaches
- Develop a quarterly resilience improvement plan
- Create a knowledge base of past incident responses