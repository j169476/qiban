"""Trusted local fixture controller, deliberately outside the SSH command set."""
import argparse
import json

import dialogue_store as fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="op", required=True)
    for op in ("grant", "revoke", "database"):
        operations.add_parser(op)
    operations.add_parser("send").add_argument("mode", choices=["confirm", "lost", "hold", "interrupt"])
    for op in ("replay", "inspect", "release"):
        operations.add_parser(op).add_argument("message")
    args = parser.parse_args()
    store = fixture.open_store()
    try:
        if args.op in ("grant", "revoke"):
            row = fixture.authorize(store, args.op == "grant")
            result = {"enabled": row["enabled"], "version": row["version"]}
        elif args.op == "send":
            result = fixture.submit(store, args.mode)
        elif args.op == "replay":
            result = fixture.replay(args.message)
        elif args.op == "release":
            with fixture.connection() as db:
                updated = db.execute("UPDATE lab_messages SET released=true WHERE id=%s AND mode='hold' AND in_model",
                                     (args.message,)).rowcount
            result = {"released": updated == 1}
        elif args.op == "inspect":
            result = fixture.inspect_message(store, args.message)
        else:
            with fixture.connection() as db:
                result = db.execute("""
                    SELECT current_database() AS database,current_user AS role,current_setting('server_version') AS version,
                           current_setting('listen_addresses') AS tcp_listeners,inet_server_addr() IS NULL AS unix_socket,
                           (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser,
                           (SELECT max(version) FROM schema_migrations) AS schema_version,
                           (SELECT count(*) FROM schema_migrations) AS migrations
                """).fetchone()
        print(json.dumps(result))
    finally:
        store.database.close()


if __name__ == "__main__":
    main()
