# MikrotTik Bandwidth Manager

Small Python service that:

- Reads active PPPoE sessions from MikroTik RouterOS REST API.
- Counts active PPPoE users.
- Divides configured total bandwidth equally.
- Sends RFC 5176 RADIUS CoA to each active session.
- Changes `Mikrotik-Rate-Limit` only.
- Does not create, delete, or directly modify MikroTik queues.

## Requirements

- Python 3.9+
- MikroTik RouterOS with REST API enabled.
- RADIUS CoA enabled on MikroTik.
- RADIUS server/secret configured to accept CoA from this application.

## MikroTik

Enable REST API, for example:

```routeros
/ip service
set www-ssl disabled=no

/radius incoming
set accept=yes port=1700

/ppp aaa
set use-radius=yes
```

Use the appropriate service/security settings for your environment.

## Configuration

Edit `config.json`.

For 10 Mbps total:

- 1 user -> 10M/10M
- 2 users -> 5M/5M
- 5 users -> 2M/2M

`Mikrotik-Rate-Limit` is encoded as Vendor ID 14988 / Vendor Type 8.

## Run

```bash
python3 pppoe_bandwidth.py
```

The program runs continuously and checks every `interval_seconds`.

## Important

The script assumes that the RADIUS CoA secret is the secret configured for the RADIUS client/NAS relationship.

Test with one PPPoE session before deploying to production.
