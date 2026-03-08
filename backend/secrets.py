import keyring

APP_NAME = "mingly"


def save_secret(key: str, value: str) -> None:
    keyring.set_password(APP_NAME, key, value)


def get_secret(key: str) -> str | None:
    return keyring.get_password(APP_NAME, key)


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(APP_NAME, key)
    except Exception:
        pass
