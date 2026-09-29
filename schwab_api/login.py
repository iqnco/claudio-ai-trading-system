"""Run once now, then again every 7 days when the token expires.

    venv312/bin/python -m schwab_api.login --manual   # on the host machine, over SSH
    venv312/bin/python -m schwab_api.login            # on a machine with a browser

--manual prints a URL. Open it in any browser (your phone or laptop), log in,
approve Claudio. You'll land on https://127.0.0.1:8182/?code=... showing an
error page; that's expected. Copy that whole URL and paste it back into the terminal.
"""
import sys
from pathlib import Path
from schwab import auth
from . import config


def main():
    Path(config.TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
    args = (config.APP_KEY(), config.APP_SECRET(), config.CALLBACK_URL, config.TOKEN_PATH)
    if "--manual" in sys.argv:
        client = auth.client_from_manual_flow(*args)
    else:
        client = auth.client_from_login_flow(*args, callback_timeout=300.0, interactive=False)
    n = len(client.get_account_numbers().json())
    print(f"Logged in. Token saved to {config.TOKEN_PATH}. {n} account(s) linked.")


if __name__ == "__main__":
    main()
