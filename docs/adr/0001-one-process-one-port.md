# One process, one container, one port

Usher runs as a single process in a single container. The Telegram bot long-polls, so it needs no public URL. One aiohttp server (port 8765 by default) serves both the admin panel and the Seerr webhook. That keeps the install to one container and one port to proxy. The cost is that login throttling and logout revocations live in memory, so they are per process and a restart forgets them.
