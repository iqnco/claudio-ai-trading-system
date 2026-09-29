# Copy to config_local.py and fill in. Never commit config_local.py.
OWNER = "there"                 # your name, used in prompts
TELEGRAM_CHAT_ID = "0"          # your numeric Telegram ID (@userinfobot); the bot only answers you

# Schwab: last-4 digits of the ONLY accounts Claudio may see or trade. Anything else is ignored.
SCHWAB_ALLOWED_ACCOUNTS = []
# Holdings Claudio must never touch (e.g. being transferred out)
SCHWAB_EXCLUDED_SYMBOLS = []
# False = dry run: the engine logs every order it would place and sends none.
SCHWAB_TRADING_ENABLED = False

# Where the daily / weekly reports go (via Resend)
REPORT_EMAIL = ""
REPORT_FROM  = "Claudio <onboarding@resend.dev>"   # use an address on your verified Resend domain

# "email" (default): only critical alerts, by email. "telegram": critical alerts on Telegram.
# "telegram_all": every fill/sell/stop move on Telegram as well.
ALERTS = "email"
