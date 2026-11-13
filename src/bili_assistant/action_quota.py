"""Units already reserved or spent on the account's configured calendar day."""


def daily_usage(db, account, day):
    rows = db.execute(
        "SELECT kind, SUM(units) used FROM actions WHERE account=? AND day=? "
        "AND status IN ('dispatching','succeeded','uncertain') GROUP BY kind",
        (account,day))
    return {row["kind"]:row["used"] for row in rows}
