# Orxaq Autonomy Reliability Enhancement Guidelines

## Resilience Design Principles

### 1. Non-Interactive Execution
- Force non-interactive environment variables
  - `CI=1`
  - `GIT_TERMINAL_PROMPT=0`
  - `PIP_NO_INPUT=1`
- Disconnect subprocess stdin
- Implement global timeout mechanisms

### 2. Git Operation Recovery
- Auto-heal stale lock files
  - Detect `.git/index.lock`, `.git/HEAD.lock`, `.git/packed-refs.lock`
  - Remove locks if no active git processes exist
  - Default stale threshold: 300 seconds
- Detect and resolve in-progress Git states
  - Identify merge conflicts, rebasing, cherry-picking
  - Safely abort or skip unresolvable states
  - Maintain comprehensive recovery logs
- Implement exponential backoff for retry
  - Base retry interval: 30 seconds
  - Maximum retry interval: 1800 seconds (30 minutes)
  - Exponential backoff formula: `delay = base * (2 ** retry_count)`
  - Track and log retry attempts with detailed diagnostics
- Prevent destructive git operations
  - Block force push to protected branches
  - Validate branch state before critical operations
  - Implement safe push mechanisms with lease and force-with-lease

### 3. Test Run Reliability
- Timeout-bound test execution
- Multi-entrypoint validation fallback
- Emit heartbeat progress updates
- Classify and handle transient failures

### 4. File Type Safety
- Preserve unknown/binary file types
- Update `.gitattributes` dynamically
- Avoid destructive rewrites

### 5. Supervisor and Runner Resilience
- Implement exponential backoff restart
- OS-level keepalive mechanisms
- Stateless, idempotent design
- Minimal runtime state dependencies

### 6. Logging and Observability
- Structured logging with trace IDs
- Conversation and lane recovery signals
- Health status API endpoints
- Filtered conversation inspection

## Recommended Implementation Strategies

### Retry Decorator
```python
def retry_with_backoff(
    max_attempts=3,
    backoff_factor=2,
    retriable_exceptions=(Exception,),
    timeout_seconds=300
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    with timeout_context(timeout_seconds):
                        return func(*args, **kwargs)
                except retriable_exceptions as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        raise
                    sleep_time = backoff_factor ** attempt
                    time.sleep(sleep_time)
        return wrapper
    return decorator
```

### Recovery Policy Configuration
```json
{
    "git_recovery": {
        "lock_file_age_threshold_seconds": 300,
        "retry_attempts": 3,
        "backoff_strategy": "exponential"
    },
    "test_run_policy": {
        "global_timeout_seconds": 1800,
        "fallback_entrypoints": [
            "pytest",
            "python -m unittest",
            "python3 -m pytest"
        ]
    }
}
```

## Monitoring and Self-Healing

1. Runtime State Tracking
   - Lane health metrics
   - Conversation recovery signals
   - Partial execution tracking

2. Automated Mitigation
   - Detect stale runners
   - Trigger supervised restart
   - Log comprehensive diagnostics

3. Security Constraints
   - Least privilege execution
   - Non-interactive mode
   - Minimal external dependencies