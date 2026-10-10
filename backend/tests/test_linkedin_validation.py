"""Tests for LinkedIn URL validation."""
import pytest

from backend.validators import validate_linkedin_url


class TestValidateLinkedinUrl:
    """Tests for the validate_linkedin_url helper."""

    def test_empty_string_returns_empty(self):
        assert validate_linkedin_url("") == ""

    def test_whitespace_only_returns_empty(self):
        assert validate_linkedin_url("   ") == ""

    def test_valid_https_url(self):
        url = "https://www.linkedin.com/in/johndoe"
        assert validate_linkedin_url(url) == url

    def test_valid_http_url(self):
        url = "http://www.linkedin.com/in/johndoe"
        assert validate_linkedin_url(url) == url

    def test_bare_domain_gets_https_prefix(self):
        result = validate_linkedin_url("linkedin.com/in/johndoe")
        assert result == "https://linkedin.com/in/johndoe"

    def test_www_bare_domain_gets_https_prefix(self):
        result = validate_linkedin_url("www.linkedin.com/in/johndoe")
        assert result == "https://www.linkedin.com/in/johndoe"

    def test_strips_whitespace(self):
        result = validate_linkedin_url("  https://www.linkedin.com/in/johndoe  ")
        assert result == "https://www.linkedin.com/in/johndoe"

    def test_company_url(self):
        url = "https://www.linkedin.com/company/acme-corp"
        assert validate_linkedin_url(url) == url

    def test_subdomain_variant(self):
        url = "https://uk.linkedin.com/in/johndoe"
        assert validate_linkedin_url(url) == url

    def test_rejects_javascript_scheme(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("javascript:alert('xss')")

    def test_rejects_data_scheme(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("data:text/html,<script>alert(1)</script>")

    def test_rejects_non_linkedin_domain(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("https://evil.com/in/johndoe")

    def test_rejects_lookalike_domain(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("https://notlinkedin.com/in/johndoe")

    def test_rejects_linkedin_in_path_only(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("https://evil.com/linkedin.com/in/johndoe")

    def test_rejects_linkedin_as_subdomain_of_other(self):
        with pytest.raises(ValueError, match="linkedin.com"):
            validate_linkedin_url("https://linkedin.com.evil.com/in/johndoe")
