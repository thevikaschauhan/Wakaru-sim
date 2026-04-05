class MiroFishError(Exception):
    """Base exception for all MiroFish client errors."""


class APIError(MiroFishError):
    """Raised when the MiroFish API returns an error response."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"API error {status_code}: {message}")


class TimeoutError(MiroFishError):
    """Raised when a polling operation exceeds the maximum wait time."""

    def __init__(self, operation: str, timeout_seconds: int):
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Operation '{operation}' timed out after {timeout_seconds}s"
        )


class SimulationError(MiroFishError):
    """Raised when a simulation enters a failed state."""

    def __init__(self, simulation_id: str, message: str):
        self.simulation_id = simulation_id
        super().__init__(f"Simulation {simulation_id} failed: {message}")
