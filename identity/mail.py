"""Handle account email transport failures without logging private mail data."""

import logging
from errno import EHOSTUNREACH, ENETDOWN, ENETUNREACH
from smtplib import SMTPException
from socket import gaierror
from ssl import SSLError

logger = logging.getLogger(__name__)


def deliver_account_email(failure_message, send, *args, **kwargs):
    try:
        send(*args, **kwargs)
    except OSError as error:
        if not isinstance(
            error, (SMTPException, ConnectionError, TimeoutError, gaierror, SSLError)
        ):
            if error.errno not in {EHOSTUNREACH, ENETDOWN, ENETUNREACH}:
                raise
        logger.warning(failure_message)
        return False
    return True
