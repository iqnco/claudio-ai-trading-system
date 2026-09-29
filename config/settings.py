# Non-secret, committed config. Secrets live in secrets_local.py, personal
# settings in config_local.py (both gitignored). Trading rules: engine/rules.py.
FUND_NAME = "Claudio Inc."

# One model for everything that needs judgment (trade review, briefs, chat).
# The engine makes ~6 calls a night; chat is the only open-ended cost.
MODEL_MAIN = "claude-sonnet-4-6"
