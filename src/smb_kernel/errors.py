"""Infrastructure failures the kernel's mechanisms raise.

Applications re-export these classes from their own error modules rather than
redefining them, so a handler written against the application's name catches
exactly what the kernel raises.
"""


class PersistenceError(Exception):
    """Raised when durable state cannot be read or written safely."""


class UnsupportedDocumentError(Exception):
    """An uploaded file is unsafe, too large, or has an unsupported type."""


class DocumentExtractionError(Exception):
    """A supported document could not produce usable plain text."""


class DocumentExtractionBusyError(Exception):
    """The bounded extraction facility has no execution or waiting capacity."""


class DocumentExtractionTimeoutError(Exception):
    """Document extraction exceeded its configured subprocess deadline."""


class AuthenticationRequiredError(Exception):
    """A request did not carry a valid authenticated identity."""


class IdentityProviderUnavailableError(Exception):
    """The configured identity provider could not validate a request."""


class ModelTransportError(Exception):
    """Safe provider failure classification independent of transport libraries."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        messages = {
            "timeout": "The model request timed out. Retry after checking model performance.",
            "rate_limit": "The model provider rate limit was reached. Wait before retrying.",
            "authentication": (
                "The model provider rejected its credentials. Check backend configuration."
            ),
            "configuration": "The provider rejected the configured model or request parameters.",
            "index_required": (
                "Knowledge search needs a current index for the configured "
                "embedding model. Contact your administrator."
            ),
            "invalid_output": (
                "The model returned invalid, empty, refused or truncated structured output."
            ),
            "payment": (
                "The model provider requires available credits or a higher key spending limit."
            ),
            "invalid_citations": (
                "The model could not provide valid source citations. Analysis was not saved."
            ),
            "unavailable": "The model provider is currently unavailable. Try again later.",
        }
        super().__init__(messages.get(kind, messages["unavailable"]))


class KnowledgeGenerationError(Exception):
    """A knowledge provider failed or returned unusable evidence."""


class ServiceUnavailableError(Exception):
    """A platform service could not be reached or answered unusably."""


class ServiceResponseError(Exception):
    """A platform service refused a request (a 4xx answer other than throttling)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class ServiceAuthenticationError(Exception):
    """An internal request carried no valid service token."""
