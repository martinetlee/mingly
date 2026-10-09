"""Input validation shared by the data layer and the API (no web framework imports)."""
import re
from urllib.parse import urlparse

_LINKEDIN_RE = re.compile(
    r'^https?://([\w-]+\.)?linkedin\.com/.*$',
    re.IGNORECASE,
)


def validate_linkedin_url(url: str) -> str:
    """Validate and normalise a LinkedIn URL.

    Accepts URLs like:
      - https://linkedin.com/in/johndoe
      - https://www.linkedin.com/in/johndoe
      - linkedin.com/in/johndoe  (will be prefixed with https://)

    Rejects anything that is not a linkedin.com URL (prevents javascript:,
    data:, or other-domain redirect attacks).

    Returns the cleaned URL string.
    Raises ValueError if the URL is invalid.
    """
    url = url.strip()
    if not url:
        return url

    # If the user typed a bare domain without scheme, prepend https://
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'

    parsed = urlparse(url)

    # Must be http or https scheme
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Invalid LinkedIn URL: must use http or https')

    # Must match linkedin.com domain
    if not _LINKEDIN_RE.match(url):
        raise ValueError('Invalid LinkedIn URL: must be a linkedin.com URL')

    return url
