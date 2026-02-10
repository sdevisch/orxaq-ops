"""Workflow management for orxaq_rpa."""

from .exceptions import RPAWorkflowError
from .config import RPASessionConfig

class RPAWorkflow:
    """Base class for RPA (Robotic Process Automation) workflows."""

    def __init__(self, config: RPASessionConfig = None):
        """
        Initialize an RPA workflow.

        :param config: Configuration for the RPA session
        """
        self.config = config or RPASessionConfig()
        self._validate_config()

    def _validate_config(self):
        """
        Validate the workflow configuration.

        :raises RPAWorkflowError: If configuration is invalid
        """
        if not self.config:
            raise RPAWorkflowError("Invalid or missing configuration")

    def execute(self):
        """
        Execute the RPA workflow.

        :raises NotImplementedError: Must be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement execute method")