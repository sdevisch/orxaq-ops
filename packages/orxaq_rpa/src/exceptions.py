"""Custom exceptions for the orxaq_rpa package."""

class RPAWorkflowError(Exception):
    """Base exception for RPA Workflow errors."""
    pass

class RPASessionError(RPAWorkflowError):
    """Exception raised for RPA session-related errors."""
    pass

class RPAConfigurationError(RPAWorkflowError):
    """Exception raised for configuration-related errors."""
    pass

class RPALaneGuardViolationError(RPAWorkflowError):
    """Exception raised when lane guard constraints are violated."""
    pass