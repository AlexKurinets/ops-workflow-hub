class HubError(Exception):
    """Base class for errors the hub raises on purpose."""


class TransientError(HubError):
    """The call may succeed later: rate limits, timeouts, 5xx responses, lock contention."""


class PermanentError(HubError):
    """The call will not succeed by retrying: validation, authorisation, a missing record."""


class DefinitionError(HubError):
    """A workflow file is malformed or references something that does not exist."""
