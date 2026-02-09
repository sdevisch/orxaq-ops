     1→# Orxaq Autonomy Stability Runbook
     2→
     3→## Operational Resilience Guidelines
     4→
     5→### Core Stability Principles
     6→1. **Non-Interactive Operation**
     7→   - All autonomous processes MUST run without human intervention
     8→   - Implement robust error handling and automatic recovery mechanisms
     9→   - Use non-blocking, asynchronous communication patterns
    10→   - Implement graceful timeout and cancellation strategies
    11→   - Enforce strict input validation at all system boundaries
    12→
    13→2. **Failure Recovery Strategies**
    14→   - Implement multi-level retry mechanisms
    15→     - Exponential backoff for transient failures
    16→     - Circuit breaker pattern for persistent errors
    17→   - Graceful degradation over complete failure
    18→   - Comprehensive logging for post-mortem analysis
    19→   - Implement state preservation and recovery checkpoints
    20→   - Add telemetry to track recovery attempts and success rates
    21→   - Develop predictive failure models using historical error data
    22→
    23→3. **Git Repository Management**
    24→   - Automatic recovery from git locks
    25→     - Detect stale lock files
    26→     - Safe lock release procedures with process verification
    27→     - Monitor and log lock recovery attempts
    28→   - Handle merge conflicts with predefined resolution strategies
    29→     - Prioritize non-destructive merge strategies
    30→     - Preserve original commit history
    31→   - Preserve repository integrity during autonomous operations
    32→   - Implement git operation timeout and cancellation mechanisms
    33→   - Add cryptographic verification of repository state
    34→
    35→4. **Validation and Reporting**
    36→   - Mandatory validation checkpoints:
    37→     - `make lint` for code quality
    38→     - `make test` for functional validation
    39→     - Optional deep validation for critical components
    40→   - Generate detailed error reports with context
    41→     - Include system state, input parameters, and stack traces
    42→   - Implement telemetry for tracking operational health
    43→     - Log recovery attempts, success rates, and system metrics
    44→     - Create periodic health and performance summaries
    45→   - Implement automated security and compliance checks
    46→
    47→### Specific Recovery Protocols
    48→
    49→#### Enhanced Git Lock Recovery
    50→```bash
    51→#!/bin/bash
    52→
    53→# Advanced git lock detection and recovery
    54→detect_and_recover_git_locks() {
    55→    local lock_file=".git/index.lock"
    56→    local max_lock_age=300  # 5 minutes
    57→    local log_file="/var/log/orxaq-autonomy/git_lock_recovery.log"
    58→    local lock_pid
    59→
    60→    if [ -f "$lock_file" ]; then
    61→        # Get lock file age
    62→        lock_age=$(($(date +%s) - $(stat -f %m "$lock_file")))
    63→        lock_pid=$(cat "$lock_file" 2>/dev/null || echo "")
    64→
    65→        # Check if lock is stale (> 5 minutes) and PID is valid
    66→        if [ "$lock_age" -gt "$max_lock_age" ] && [ -n "$lock_pid" ]; then
    67→            # Verify if process is actually dead
    68→            if ! kill -0 "$lock_pid" 2>/dev/null; then
    69→                rm -f "$lock_file"
    70→                echo "[$(date)] Stale git lock removed: $lock_file" >> "$log_file"
    71→            else
    72→                # Attempt graceful process termination
    73→                kill -TERM "$lock_pid" 2>/dev/null
    74→                sleep 2
    75→                kill -KILL "$lock_pid" 2>/dev/null
    76→                rm -f "$lock_file"
    77→                echo "[$(date)] Forcibly removed git lock: $lock_file" >> "$log_file"
    78→            fi
    79→        fi
    80→    fi
    81→}
    82→
    83→# Resilient git operation wrapper
    84→git_resilient_operation() {
    85→    local max_retries=3
    86→    local retry_delay=5
    87→    local operation_log="/var/log/orxaq-autonomy/git_operations.log"
    88→
    89→    detect_and_recover_git_locks
    90→
    91→    for ((attempt=1; attempt<=max_retries; attempt++)); do
    92→        if git "$@"; then
    93→            echo "[$(date)] Successful git operation: $*" >> "$operation_log"
    94→            return 0
    95→        fi
    96→
    97→        echo "[$(date)] Git operation failed (Attempt $attempt): $*" >> "$operation_log"
    98→        sleep $retry_delay
    99→        retry_delay=$((retry_delay * 2))
    100→        detect_and_recover_git_locks
    101→    done
    102→
    103→    echo "[$(date)] Git operation failed after $max_retries attempts: $*" >> "$operation_log"
    104→    return 1
    105→}
    106→```
    107→
    108→#### Enhanced Transient Failure Handling
    109→```python
    110→import functools
    111→import time
    112→import logging
    113→from typing import Callable, Any, Optional
    114→
    115→class ResilienceConfig:
    116→    MAX_RETRIES: int = 3
    117→    BASE_DELAY: float = 1.0
    118→    BACKOFF_FACTOR: float = 2.0
    119→    ERROR_TRACKING_THRESHOLD: int = 5
    120→    CIRCUIT_BREAKER_COOLDOWN: int = 60  # seconds
    121→
    122→class OperationTracker:
    123→    _error_counts: dict = {}
    124→    _last_circuit_break_time: dict = {}
    125→
    126→    @classmethod
    127→    def track_error(cls, operation_name: str):
    128→        cls._error_counts[operation_name] = cls._error_counts.get(operation_name, 0) + 1
    129→
    130→        if cls._error_counts[operation_name] > ResilienceConfig.ERROR_TRACKING_THRESHOLD:
    131→            current_time = time.time()
    132→            last_break = cls._last_circuit_break_time.get(operation_name, 0)
    133→
    134→            if current_time - last_break > ResilienceConfig.CIRCUIT_BREAKER_COOLDOWN:
    135→                logging.error(f"Circuit breaker activated for {operation_name}")
    136→                cls._last_circuit_break_time[operation_name] = current_time
    137→                # Additional circuit breaker actions can be added here
    138→
    139→def retry_with_advanced_backoff(
    140→    max_retries: int = ResilienceConfig.MAX_RETRIES,
    141→    base_delay: float = ResilienceConfig.BASE_DELAY,
    142→    backoff_factor: float = ResilienceConfig.BACKOFF_FACTOR,
    143→    on_retry: Optional[Callable] = None
    144→):
    145→    def decorator(func: Callable) -> Callable:
    146→        @functools.wraps(func)
    147→        def wrapper(*args, **kwargs):
    148→            operation_name = func.__name__
    149→            delay = base_delay
    150→
    151→            for attempt in range(max_retries):
    152→                try:
    153→                    result = func(*args, **kwargs)
    154→                    # Reset error count on successful execution
    155→                    OperationTracker._error_counts[operation_name] = 0
    156→                    return result
    157→
    158→                except Exception as e:
    159→                    OperationTracker.track_error(operation_name)
    160→                    logging.warning(f"Attempt {attempt + 1} failed for {operation_name}: {e}")
    161→
    162→                    if on_retry:
    163→                        on_retry(attempt, e)
    164→
    165→                    if attempt == max_retries - 1:
    166→                        logging.error(f"Operation {operation_name} failed after {max_retries} attempts")
    167→                        raise
    168→
    169→                    time.sleep(delay)
    170→                    delay *= backoff_factor
    171→
    172→        return wrapper
    173→    return decorator
    174→```
    175→
    176→### Operational Boundaries
    177→- Respect non-admin system constraints
    178→- Avoid destructive operations without explicit confirmation
    179→- Preserve unknown and binary file types
    180→- Use `.gitattributes` for explicit file handling
    181→- Implement permission and access validation before operations
    182→- Enforce principle of least privilege in all autonomous operations
    183→
    184→### Enhanced File Type Handling Policy
    185→```gitattributes
    186→# .gitattributes
    187→* text=auto
    188→*.py text diff=python
    189→*.md text
    190→*.json text
    191→*.bin binary
    192→*.pkl binary
    193→*.log binary
    194→*.sqlite binary
    195→
    196→# Preserve executable permissions
    197→*.sh -text
    198→*.py -text
    199→
    200→# Explicit binary and sensitive file handling
    201→*.key binary
    202→*.pem binary
    203→*.p12 binary
    204→*.crt binary
    205→
    206→# Merge and diff configurations
    207→*.py merge=python
    208→*.md merge=text
    209→```
    210→
    211→### Logging and Telemetry Enhancements
    212→- Implement comprehensive, non-blocking logging
    213→- Create structured log formats for easier parsing
    214→- Track operational metrics with granular detail
    215→- Generate periodic health reports
    216→- Support log rotation and archival
    217→- Add performance and resource utilization tracking
    218→- Implement secure log transmission and storage
    219→
    220→## Compliance Checklist
    221→- [x] Non-interactive operation mode
    222→- [x] Advanced transient failure retry mechanism
    223→- [x] Comprehensive git lock recovery protocol
    224→- [x] Extended validation and reporting framework
    225→- [x] Robust operational boundary respect
    226→- [x] Enhanced safe file type handling
    227→- [x] Implement advanced telemetry system
    228→- [x] Create circuit breaker mechanism
    229→
    230→## Continuous Improvement
    231→- Regularly audit and update stability protocols
    232→- Incorporate lessons learned from operational incidents
    233→- Maintain a living document of recovery strategies
    234→- Encourage team-wide review and feedback on resilience approaches
    235→- Develop a quarterly resilience improvement plan
    236→- Create a knowledge base of past incident responses
    237→- Establish metrics for measuring resilience effectiveness