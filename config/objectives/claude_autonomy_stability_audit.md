# Orxaq Autonomy Stability Runbook

## Operational Resilience Guidelines

### Core Stability Principles
1. **Non-Interactive Operation**
   - All autonomous processes MUST run without human intervention
   - Implement robust error handling and automatic recovery mechanisms
   - Use non-blocking, asynchronous communication patterns

2. **Failure Recovery Strategies**
   - Implement multi-level retry mechanisms
     - Exponential backoff for transient failures
     - Circuit breaker pattern for persistent errors
   - Graceful degradation over complete failure
   - Comprehensive logging for post-mortem analysis

3. **Git Repository Management**
   - Automatic recovery from git locks
     - Detect stale lock files
     - Safe lock release procedures
   - Handle merge conflicts with predefined resolution strategies
   - Preserve repository integrity during autonomous operations

4. **Validation and Reporting**
   - Mandatory validation checkpoints:
     - `make lint` for code quality
     - `make test` for functional validation
   - Generate detailed error reports
   - Implement telemetry for tracking operational health

### Specific Recovery Protocols

#### Git Lock Recovery
```bash
# Detect and safely remove stale git locks
detect_git_locks() {
    # Check for stale lock files
    if [ -f .git/index.lock ]; then
        # Verify process is actually dead
        LOCK_PID=$(cat .git/index.lock)
        if ! kill -0 $LOCK_PID 2>/dev/null; then
            rm -f .git/index.lock
            log "Stale git lock removed safely"
        fi
    fi
}

# Retry git operations with resilience
git_resilient_operation() {
    max_retries=3
    retry_delay=5

    for ((i=1; i<=max_retries; i++)); do
        detect_git_locks
        if git "$@"; then
            return 0
        fi

        sleep $retry_delay
        retry_delay=$((retry_delay * 2))
    done

    log "Git operation failed after $max_retries attempts"
    return 1
}
```

#### Transient Failure Handling
```python
import functools
import time
import logging

def retry_with_backoff(max_retries=3, base_delay=1, backoff_factor=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logging.warning(f"Attempt {attempt + 1} failed: {e}")
                    if attempt == max_retries - 1:
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

### File Type Handling Policy
```gitattributes
# .gitattributes
* text=auto
*.py text diff=python
*.md text
*.json text
*.bin binary
*.pkl binary
```

### Logging and Telemetry
- Implement comprehensive, non-blocking logging
- Track operational metrics
- Generate periodic health reports

## Compliance Checklist
- [x] Non-interactive operation mode
- [x] Transient failure retry mechanism
- [x] Git lock recovery protocol
- [x] Validation and reporting framework
- [x] Operational boundary respect
- [x] Safe file type handling

## Continuous Improvement
- Regularly audit and update stability protocols
- Incorporate lessons learned from operational incidents
- Maintain a living document of recovery strategies