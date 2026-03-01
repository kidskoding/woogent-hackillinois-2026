#!/bin/bash
set -e

echo "Waiting for WordPress to be installed..."
sleep 15

cd /var/www/html

# Install WordPress if not already done
if ! wp core is-installed --allow-root 2>/dev/null; then
  echo "Installing WordPress..."
  wp core install \
    --url="${WP_SITEURL:-http://localhost:8080}" \
    --title="WooGent Demo Store" \
    --admin_user="admin" \
    --admin_password="adminpassword" \
    --admin_email="admin@woogent.local" \
    --skip-email \
    --allow-root
else
  echo "WordPress already installed."
fi

# Install and activate WooCommerce (check filesystem, not just DB, so fresh deploys work)
if [ ! -d "wp-content/plugins/woocommerce" ]; then
  echo "Installing WooCommerce..."
  wp plugin install woocommerce --activate --allow-root
  echo "Running WooCommerce setup..."
  wp wc --user=admin tool run install_pages --allow-root 2>/dev/null || true
else
  echo "WooCommerce already installed."
fi

# Disable WooCommerce "Coming Soon" mode so the store is publicly accessible
wp option update woocommerce_coming_soon no --allow-root 2>/dev/null || true

# Enable HPOS (High-Performance Order Storage) — both the feature flag AND the active switch
wp option update woocommerce_feature_custom_order_tables_enabled yes --allow-root 2>/dev/null || true
wp option update woocommerce_custom_orders_table_enabled yes --allow-root 2>/dev/null || true
# Run WooCommerce installer to create/migrate HPOS tables
wp eval 'WC_Install::install();' --user=admin --allow-root 2>/dev/null || true

# Import sample products if not already imported (or force re-import for prod)
PRODUCT_COUNT=$(wp post list --post_type=product --format=count --allow-root 2>/dev/null || echo "0")
if [ "$PRODUCT_COUNT" -lt "5" ] || [ "$FORCE_PRODUCT_REIMPORT" = "true" ]; then
  if [ "$FORCE_PRODUCT_REIMPORT" = "true" ] && [ "$PRODUCT_COUNT" -gt "0" ]; then
    echo "FORCE_PRODUCT_REIMPORT=true: deleting $PRODUCT_COUNT existing products and re-importing..."
    wp post delete $(wp post list --post_type=product --format=ids --allow-root) --force --allow-root 2>/dev/null || true
  fi
  echo "Importing WooCommerce sample data..."
  wp plugin install wordpress-importer --activate --allow-root
  curl -sL "https://raw.githubusercontent.com/woocommerce/woocommerce/trunk/plugins/woocommerce/sample-data/sample_products.xml" \
    -o /tmp/woo-sample.xml
  wp import /tmp/woo-sample.xml --authors=create --allow-root
  echo "Sample data imported."
else
  echo "Products already exist ($PRODUCT_COUNT found), skipping import."
fi

# Install and configure Stripe payment gateway (check filesystem for fresh deploys)
if [ ! -d "wp-content/plugins/woocommerce-gateway-stripe" ]; then
  echo "Installing WooCommerce Stripe plugin..."
  wp plugin install woocommerce-gateway-stripe --activate --allow-root
else
  echo "Stripe plugin already installed."
fi

if [ -n "$STRIPE_TEST_KEY" ] && [ -n "$STRIPE_PUBLISHABLE_KEY" ]; then
  echo "Configuring Stripe test keys..."
  wp eval '
update_option("woocommerce_stripe_settings", array(
    "enabled"              => "yes",
    "testmode"             => "yes",
    "test_secret_key"      => getenv("STRIPE_TEST_KEY"),
    "test_publishable_key" => getenv("STRIPE_PUBLISHABLE_KEY"),
    "secret_key"           => "",
    "publishable_key"      => "",
    "title"                => "Credit Card (Stripe)",
    "capture"              => "yes",
    "payment_request"      => "yes",
    "saved_cards"          => "yes",
));
' --allow-root 2>/dev/null || true
fi

# Update site URL if WP_SITEURL is set and differs from what's in the DB
# This handles production deploys where the host is not localhost:8080
WP_SITEURL="${WP_SITEURL:-http://localhost:8080}"
CURRENT_URL=$(wp option get siteurl --allow-root 2>/dev/null || echo "")
if [ -n "$CURRENT_URL" ] && [ "$CURRENT_URL" != "$WP_SITEURL" ]; then
  echo "Migrating WordPress URL: $CURRENT_URL → $WP_SITEURL"
  wp search-replace "$CURRENT_URL" "$WP_SITEURL" --all-tables --allow-root 2>/dev/null || true
fi

# Set up permalink structure for clean URLs
wp rewrite structure '/%postname%/' --allow-root
wp rewrite flush --allow-root

echo "WooCommerce setup complete!"
echo "Admin: $WP_SITEURL/wp-admin (admin/adminpassword)"
