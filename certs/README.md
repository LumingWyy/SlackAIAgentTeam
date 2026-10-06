# Extra root CAs for the Docker image

If your network inspects TLS (for example Cloudflare Zero Trust / Gateway, a
corporate proxy or antivirus HTTPS scanning), HTTPS inside the container fails
with "self-signed certificate in certificate chain": the build cannot reach
NodeSource or npm, and at run time Claude, Slack and GitHub calls fail.

Put that network's root CA here as a PEM file named `<anything>.crt`. The image
adds it to the system trust store, which apt, curl, pip, Python and Node use.
Only `*.crt` files reach the Docker build (`.dockerignore`), and they are
gitignored. Put public CA certificates here only, never a private key.

On macOS, export a CA that the system already trusts, e.g. Cloudflare Gateway:

    security find-certificate -c "Gateway CA - Cloudflare" -p \
      /Library/Keychains/System.keychain > certs/cloudflare-gateway.crt

Without any file here the image is built exactly as before.
