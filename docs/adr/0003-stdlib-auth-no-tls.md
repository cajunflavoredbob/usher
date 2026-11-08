# Admin panel auth uses only the standard library, and Usher does not terminate TLS

Panel passwords, session cookies and CSRF tokens are built from the Python standard library (PBKDF2 and HMAC), not from an auth framework. Changing the password signs out every other session. Usher serves plain HTTP. TLS belongs to a reverse proxy, and forwarded client addresses are only trusted from proxies listed in TRUSTED_PROXIES.
