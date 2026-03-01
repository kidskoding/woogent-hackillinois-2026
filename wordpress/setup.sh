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
FORCE_REIMPORT=$(echo "${FORCE_PRODUCT_REIMPORT:-}" | tr '[:upper:]' '[:lower:]')
if [ "$PRODUCT_COUNT" -lt "5" ] || [ "$FORCE_REIMPORT" = "true" ] || [ "$FORCE_REIMPORT" = "1" ] || [ "$FORCE_REIMPORT" = "yes" ]; then
  if { [ "$FORCE_REIMPORT" = "true" ] || [ "$FORCE_REIMPORT" = "1" ] || [ "$FORCE_REIMPORT" = "yes" ]; } && [ "$PRODUCT_COUNT" -gt "0" ]; then
    echo "FORCE_PRODUCT_REIMPORT: deleting $PRODUCT_COUNT existing products and re-importing..."
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

# Apply custom product descriptions (always, so re-imports get patched too)
echo "Applying custom product descriptions..."
cat > /tmp/update-descriptions.php << 'PHP'
<?php
$products = [
  'v-neck-t-shirt' => [
    'content' => 'A wardrobe essential. Our V-Neck T-Shirt is cut from breathable, 100% combed cotton for all-day comfort. The relaxed v-neck sits just right — not too deep, not too shallow. Available in three colors and three sizes.',
    'excerpt' => 'A lightweight v-neck tee available in Red, Blue, and Green. Sizes from Small to Large.',
  ],
  'hoodie' => [
    'content' => 'Stay warm and stylish in our classic pullover hoodie. Made from a soft cotton-polyester blend, it features a kangaroo pocket, adjustable drawstring hood, and ribbed cuffs. Available in Red, Blue, and Green, with an optional embroidered logo.',
    'excerpt' => 'A cozy pullover hoodie available in Red, Blue, and Green — with or without a logo. Perfect for everyday wear.',
  ],
  'hoodie-with-logo' => [
    'content' => 'Our Hoodie with Logo features the same soft cotton-polyester blend as our classic hoodie, with the addition of our signature embroidered logo on the chest. A clean, minimal look that never goes out of style.',
    'excerpt' => 'A classic pullover hoodie featuring our signature embroidered logo. Warm, comfortable, and iconic.',
  ],
  't-shirt' => [
    'content' => 'Clean lines, soft fabric, perfect fit. Our everyday T-Shirt is made from 100% ring-spun cotton and pre-shrunk for a consistent fit wash after wash. Pairs with everything.',
    'excerpt' => 'A simple, versatile crew-neck tee crafted from soft 100% cotton. A true wardrobe staple.',
  ],
  'beanie' => [
    'content' => 'Our classic ribbed beanie is knit from a warm acrylic blend with a stretchy fit that suits most head sizes. Fold up the cuff for a shorter style or wear it slouchy — it works both ways.',
    'excerpt' => 'A soft ribbed beanie to keep you warm in style. One size fits most.',
  ],
  'belt' => [
    'content' => 'Crafted from full-grain leather, this belt develops a rich patina over time. The polished silver buckle adds a refined touch that works equally well with jeans or dress trousers.',
    'excerpt' => 'A durable leather belt with a polished silver buckle. Fits waist sizes 28–44.',
  ],
  'cap' => [
    'content' => 'This structured snapback cap features a pre-curved brim, breathable eyelets, and an embroidered logo on the front. The plastic snapback closure provides an adjustable, comfortable fit.',
    'excerpt' => 'A structured snapback cap with an embroidered logo. Adjustable fit for all head sizes.',
  ],
  'sunglasses' => [
    'content' => 'Block out the sun in style. These wraparound sunglasses feature polycarbonate lenses with UV400 protection, a lightweight frame, and non-slip nose pads. Suitable for sport and everyday wear.',
    'excerpt' => 'Lightweight wraparound sunglasses with UV400 protection. Style meets function.',
  ],
  'hoodie-with-pocket' => [
    'content' => 'Everything you love about a hoodie — minus the zipper. Our Hoodie with Pocket features a large front kangaroo pocket, a cozy fleece interior, and a clean silhouette that looks great on its own or layered.',
    'excerpt' => 'A zip-free hoodie with a handy kangaroo pocket. Soft, warm, and minimal.',
  ],
  'hoodie-with-zipper' => [
    'content' => 'Our Hoodie with Zipper gives you the flexibility of a jacket with the comfort of a hoodie. The full-length metal zipper, two side pockets, and ribbed hem make it perfect for the gym, commute, or weekend.',
    'excerpt' => 'A full-zip hoodie with a sleek metal zipper and side pockets. Versatile and warm.',
  ],
  'long-sleeve-tee' => [
    'content' => 'When a t-shirt is not quite enough, reach for our Long Sleeve Tee. Made from the same soft ring-spun cotton as our classic tee, with a slightly relaxed fit and ribbed cuffs for a clean finish.',
    'excerpt' => 'A relaxed long-sleeve tee in breathable cotton. Lightweight coverage for cooler days.',
  ],
  'polo' => [
    'content' => 'Our pique polo is made from a breathable cotton-polyester blend that holds its shape and resists wrinkles. The two-button placket, ribbed collar, and side vents make it as comfortable as it is polished.',
    'excerpt' => 'A classic pique polo shirt with a two-button placket. Smart casual done right.',
  ],
  'album' => [
    'content' => 'A complete studio album available as an instant digital download in high-quality MP3 and FLAC formats. No shipping, no waiting — just great music delivered straight to your inbox.',
    'excerpt' => 'A full-length digital album — instant download upon purchase.',
  ],
  'single' => [
    'content' => 'Download this single track instantly in MP3 and FLAC formats. High-quality audio, delivered immediately after purchase.',
    'excerpt' => 'A single digital track — instant download upon purchase.',
  ],
  't-shirt-with-logo' => [
    'content' => 'Same great fit as our everyday T-Shirt, now with a bold embroidered logo on the left chest. Made from 100% ring-spun cotton and pre-shrunk for consistent sizing.',
    'excerpt' => 'Our classic T-Shirt now featuring a bold embroidered logo on the chest.',
  ],
  'beanie-with-logo' => [
    'content' => 'The same comfortable ribbed beanie you love, now with a small woven logo badge on the cuff. A clean, minimal detail that makes all the difference.',
    'excerpt' => 'Our cozy ribbed beanie with an embroidered logo badge. A subtle signature touch.',
  ],
  'logo-collection' => [
    'content' => 'The Logo Collection bundles our most popular logo products together. Perfect as a gift or for those who want the full set. Includes the Logo Cap, Logo Tee, and Logo Beanie.',
    'excerpt' => 'A curated collection of our logo items — everything you need to rep the brand.',
  ],
  'wordpress-pennant' => [
    'content' => 'This classic felt pennant measures 18 inches and is perfect for decorating a bedroom, office, or fan cave. Lightweight and easy to hang, it makes a great gift for any enthusiast.',
    'excerpt' => 'A felt pennant flag perfect for decorating your space or showing team spirit.',
  ],
];
foreach ($products as $slug => $data) {
  $post = get_page_by_path($slug, OBJECT, 'product');
  if ($post) {
    wp_update_post(['ID' => $post->ID, 'post_content' => $data['content'], 'post_excerpt' => $data['excerpt']]);
    echo "Updated: $slug\n";
  } else {
    echo "Not found: $slug\n";
  }
}

// Fix product images by re-linking existing attachment files to the correct products.
// After a re-import the CDN downloads can fail, leaving broken attachment records.
// We match by basename so the bind-mounted uploads files always win.
$product_images = [
  'v-neck-t-shirt'    => ['thumb' => 'vneck-tee-2.jpg',         'gallery' => ['vnech-tee-green-1.jpg', 'vnech-tee-blue-1.jpg']],
  'hoodie'            => ['thumb' => 'hoodie-2.jpg',             'gallery' => ['hoodie-blue-1.jpg', 'hoodie-green-1.jpg', 'hoodie-with-logo-2.jpg']],
  'hoodie-with-logo'  => ['thumb' => 'hoodie-with-logo-2.jpg',   'gallery' => []],
  't-shirt'           => ['thumb' => 'tshirt-2.jpg',             'gallery' => []],
  'beanie'            => ['thumb' => 'beanie-2.jpg',             'gallery' => []],
  'belt'              => ['thumb' => 'belt-2.jpg',               'gallery' => []],
  'cap'               => ['thumb' => 'cap-2.jpg',                'gallery' => []],
  'sunglasses'        => ['thumb' => 'sunglasses-2.jpg',         'gallery' => []],
  'hoodie-with-pocket'=> ['thumb' => 'hoodie-with-pocket-2.jpg', 'gallery' => []],
  'hoodie-with-zipper'=> ['thumb' => 'hoodie-with-zipper-2.jpg', 'gallery' => []],
  'long-sleeve-tee'   => ['thumb' => 'long-sleeve-tee-2.jpg',   'gallery' => []],
  'polo'              => ['thumb' => 'polo-2.jpg',               'gallery' => []],
  'album'             => ['thumb' => 'album-1.jpg',              'gallery' => []],
  'single'            => ['thumb' => 'single-1.jpg',             'gallery' => []],
  't-shirt-with-logo' => ['thumb' => 't-shirt-with-logo-1.jpg',  'gallery' => []],
  'beanie-with-logo'  => ['thumb' => 'beanie-with-logo-1.jpg',   'gallery' => []],
  'logo-collection'   => ['thumb' => 'logo-1.jpg',               'gallery' => []],
  'wordpress-pennant' => ['thumb' => 'pennant-1.jpg',            'gallery' => []],
];
// Build filename -> attachment_id index
$att_cache = [];
$att_posts = get_posts(['post_type' => 'attachment', 'numberposts' => -1, 'post_status' => 'inherit']);
foreach ($att_posts as $att) {
  $file = basename(get_attached_file($att->ID));
  if ($file) $att_cache[$file] = $att->ID;
}
foreach ($product_images as $slug => $images) {
  $post = get_page_by_path($slug, OBJECT, 'product');
  if (!$post) continue;
  if (!empty($att_cache[$images['thumb']])) {
    update_post_meta($post->ID, '_thumbnail_id', $att_cache[$images['thumb']]);
  }
  $gallery_ids = [];
  foreach ($images['gallery'] as $gfile) {
    if (!empty($att_cache[$gfile])) $gallery_ids[] = $att_cache[$gfile];
  }
  update_post_meta($post->ID, '_product_image_gallery', implode(',', $gallery_ids));
  echo "Fixed images: $slug\n";
}
PHP
wp eval-file /tmp/update-descriptions.php --allow-root 2>/dev/null || true
echo "Product descriptions and images applied."

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
