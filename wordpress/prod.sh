#!/bin/bash
# Run this on the prod server to fix broken images.
# Usage: docker compose run --rm wpcli bash /prod.sh
# Set WP_SITEURL in .env first, e.g. WP_SITEURL=http://159.65.188.106:8080

set -e
PROD_URL="${WP_SITEURL:-$PROD_URL}"
if [ -z "$PROD_URL" ]; then
  echo "Set WP_SITEURL or PROD_URL in .env, e.g. WP_SITEURL=http://159.65.188.106:8080"
  exit 1
fi

cd /var/www/html
CURRENT=$(wp option get siteurl --allow-root 2>/dev/null || echo "http://localhost:8080")
echo "Replacing $CURRENT → $PROD_URL"
wp search-replace "$CURRENT" "$PROD_URL" --all-tables --allow-root
wp search-replace "http://localhost:8080" "$PROD_URL" --all-tables --allow-root 2>/dev/null || true
wp rewrite flush --allow-root
echo "Done. Reload the store."
