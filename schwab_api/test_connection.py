"""Read-only smoke test. Places no orders.

    venv312/bin/python -m schwab_api.test_connection
"""
from .client import get_client, account_hashes, token_status


def main():
    print("Token:", token_status())
    c = get_client()

    for last4, h in account_hashes(c).items():
        r = c.get_account(h, fields=[c.Account.Fields.POSITIONS])
        r.raise_for_status()
        acct = r.json()["securitiesAccount"]
        bal = acct.get("currentBalances", {})
        print(f"\n...{last4} ({acct.get('type')}): "
              f"liquidation ${bal.get('liquidationValue', 0):,.2f}, "
              f"cash ${bal.get('cashBalance', 0):,.2f}")
        for p in acct.get("positions", []):
            print(f"   {p['instrument'].get('symbol', '?'):<8} {p.get('longQuantity', 0):>10g} "
                  f"  mkt ${p.get('marketValue', 0):,.2f}")

    q = c.get_quote("SPY")
    q.raise_for_status()
    print("\nSPY last:", q.json()["SPY"]["quote"]["lastPrice"])
    print("\nAll good. Claudio can read accounts and market data.")


if __name__ == "__main__":
    main()
