"""Configuration management for orxaq_rpa."""

class RPASessionConfig:
    """Configuration class for RPA sessions."""

    def __init__(self,
                 workflow_type=None,
                 session_timeout=3600,
                 retry_attempts=3):
        """
        Initialize RPA session configuration.

        :param workflow_type: Type of workflow to be used
        :param session_timeout: Maximum session duration in seconds
        :param retry_attempts: Number of retry attempts for transient failures
        """
        self.workflow_type = workflow_type
        self.session_timeout = session_timeout
        self.retry_attempts = retry_attempts