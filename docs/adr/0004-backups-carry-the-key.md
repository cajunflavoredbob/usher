# Backups include the encryption key, so a backup is a secret

Plex sign-in tokens are encrypted at rest with a key kept in the data folder, unless USHER_ENCRYPTION_KEY supplies it. A backup bundles settings, the database and that key file when it exists, and a restore writes back whichever of them the backup contains. That makes the backup file as sensitive as the server itself. Backups can be wrapped with a passphrase, and making one without a passphrase needs an explicit acknowledgement.
