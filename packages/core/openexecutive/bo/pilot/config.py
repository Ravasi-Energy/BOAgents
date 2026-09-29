"""Synthetic-only destination policy; no DNS, proxy inheritance or redirects."""
import json
import re
from urllib.parse import urlsplit


def endpoint(value):
    from openexecutive.bo.settings.registry import SettingValidationError
    if value == "":
        return value
    if not isinstance(value, str):
        raise SettingValidationError("Endpoint sintetic: text obligatoriu")
    try:
        url = urlsplit(value)
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1"
                 and url.port and 1024 <= url.port <= 65535
                 and not url.username and not url.password
                 and not url.query and not url.fragment and url.path == ""
                 and value == f"http://127.0.0.1:{url.port}")
    except ValueError:
        valid = False
    if not valid:
        raise SettingValidationError("Pilotul permite numai http://127.0.0.1:PORT explicit, fără cale sau credentiale")
    return value


def allowlist(value):
    from openexecutive.bo.settings.registry import SettingValidationError
    try:
        values = json.loads(value)
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError
        for item in values:
            if not item:
                raise ValueError
            endpoint(item)
    except (ValueError, TypeError):
        raise SettingValidationError("Allowlist: listă JSON de maximum 8 endpointuri sintetice explicite") from None
    return json.dumps(values)


def secret_ref(value):
    from openexecutive.bo.settings.registry import SettingValidationError
    if not isinstance(value, str) or not re.fullmatch(r"BO_PILOT_[A-Z0-9_]{1,64}", value):
        raise SettingValidationError("SecretRef trebuie să fie un nume BO_PILOT_…; nu valoarea secretului")
    return value
