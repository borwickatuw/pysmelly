"""Tests for logging smell checks."""

from pysmelly.checks.logging_smells import (
    check_logging_config_hijack,
    check_secrets_in_logs,
)
from pysmelly.registry import Severity


class TestSecretsInLogs:
    def test_finds_secret_in_fstring(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def charge(card_number, cvv):
    logger.info(f"charging {card_number} (cvv {cvv})")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1
        assert "card_number" in findings[0].message
        assert "cvv" in findings[0].message
        assert findings[0].severity == Severity.HIGH

    def test_finds_secret_positional_arg(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def login(user, password):
    logger.debug("login attempt %s %s", user, password)
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1
        assert "password" in findings[0].message

    def test_finds_secret_attribute(self, trees):
        t = trees.code("""\
import logging
log = logging.getLogger(__name__)

def sync(account):
    log.warning(f"retrying with {account.api_key}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1
        assert "api_key" in findings[0].message

    def test_finds_secret_dict_key(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def register(data):
    logger.info(f"new user {data['password']}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1
        assert "password" in findings[0].message

    def test_non_secret_names_ignored(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def process(user, order_id):
    logger.info(f"processing order {order_id} for {user}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_token_counts_not_flagged(self, trees):
        """Plural "tokens" segments (LLM usage counts) are not secrets."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def report(max_tokens, total_tokens):
    logger.info(f"used {total_tokens} of {max_tokens}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_s3_key_not_flagged(self, trees):
        """Bare "key" segments (object keys, dict keys) are not secrets."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def upload(s3_key):
    logger.info(f"uploading to {s3_key}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_metadata_suffix_not_flagged(self, trees):
        """token_id / api_key_name are identifiers ABOUT secrets, not
        the secrets themselves."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def register(token_id, api_key_name):
    logger.debug(f"registered upload for token {token_id} ({api_key_name})")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_metadata_attribute_read_not_flagged(self, trees):
        """api_token.name extracts a non-secret field from a
        secret-named object."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def deposit(api_token):
    logger.info(f"deposit via token: {api_token.name}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_nonsecret_dict_field_not_flagged(self, trees):
        """token["scope"] / token.get("scope") extract non-secret fields."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def callback(token):
    logger.info("scopes=%s", token.get("scope", "(none)"))
    logger.info("kind=%s", token["kind"])
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_sanitized_secret_not_flagged(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def check(password, card_number):
    logger.info(f"password length {len(password)}, card {mask(card_number)}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_method_call_on_secret_still_flagged(self, trees):
        """Transforming the secret (strip/upper) doesn't sanitize it."""
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def check(card_number):
    logger.info(f"card {card_number.strip()}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1

    def test_auth_token_flagged(self, trees):
        t = trees.code("""\
import logging
logger = logging.getLogger(__name__)

def call_api(auth_token):
    logger.error(f"request failed with {auth_token}")
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 1

    def test_non_logger_calls_ignored(self, trees):
        """Same method names on non-logger receivers don't count."""
        t = trees.code("""\
def notify(client, password):
    client.error(password)
    client.info(password)
""")
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "tests/test_auth.py": """\
import logging
logger = logging.getLogger(__name__)

def test_login(password):
    logger.info(f"testing with {password}")
"""
            }
        )
        findings = check_secrets_in_logs(t)
        assert len(findings) == 0


class TestLoggingConfigHijack:
    def test_module_scope_basicconfig_in_library(self, trees):
        t = trees.code("""\
import logging

logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger(__name__)
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 1
        assert "basicConfig" in findings[0].message
        assert findings[0].line == 3

    def test_basicconfig_with_main_guard_ok(self, trees):
        """A module with a __main__ guard is a script, not a library."""
        t = trees.code("""\
import logging

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    print("running")
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_basicconfig_inside_function_ok(self, trees):
        t = trees.code("""\
import logging

def main():
    logging.basicConfig(level=logging.INFO)
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_addhandler_in_function_body(self, trees):
        t = trees.code("""\
import logging

def get_logger():
    log = logging.getLogger("app")
    log.addHandler(logging.StreamHandler())
    return log
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 1
        assert "addHandler" in findings[0].message

    def test_guarded_addhandler_ok(self, trees):
        t = trees.code("""\
import logging

def get_logger():
    log = logging.getLogger("app")
    if not log.handlers:
        log.addHandler(logging.StreamHandler())
    return log
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_module_scope_addhandler_ok(self, trees):
        """Module-scope addHandler runs once per import — normal setup."""
        t = trees.code("""\
import logging

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_setup_function_addhandler_ok(self, trees):
        """setup_logging()/main() are the once-per-process config place
        the check's own advice points to."""
        t = trees.code("""\
import logging

def setup_logging():
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())

def main():
    logging.getLogger().addHandler(logging.StreamHandler())
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_once_flag_guarded_addhandler_ok(self, trees):
        """A once-flag early return makes the function idempotent."""
        t = trees.code("""\
import logging

_attached = False

def _attach_file_handler():
    global _attached
    if _attached:
        return
    logging.getLogger().addHandler(logging.FileHandler("x.log"))
    _attached = True
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0

    def test_nullhandler_ok(self, trees):
        """addHandler(NullHandler()) is the recommended library pattern."""
        t = trees.code("""\
import logging

def setup():
    logger = logging.getLogger(__name__)
    logger.addHandler(logging.NullHandler())
""")
        findings = check_logging_config_hijack(t)
        assert len(findings) == 0
