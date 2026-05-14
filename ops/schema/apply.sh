#!/usr/bin/env bash
# =====================================================================
# Betty schema migration runner
# =====================================================================
# Applies any .sql files in this directory that haven't been recorded
# in the schema_migrations table yet. Idempotent — safe to run any time.
#
# Usage: ./apply.sh
# =====================================================================

set -euo pipefail

SCHEMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="betty-postgres"
DB_USER="betty"
DB_NAME="betty"

echo "→ Applying migrations from: $SCHEMA_DIR"

# Verify container is up
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "✗ Container '${CONTAINER}' is not running."
    echo "  Start it with: cd ~/code/betty/docker && docker compose up -d"
    exit 1
fi

# Apply each migration in lexical order
applied_count=0
for sql_file in "$SCHEMA_DIR"/[0-9][0-9][0-9]_*.sql; do
    [ -f "$sql_file" ] || continue
    filename=$(basename "$sql_file")
    version="${filename%%_*}"

    echo "  • Checking ${filename}..."

    already_applied=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='${version}')" 2>/dev/null || echo "f")

    if [ "$already_applied" = "t" ]; then
        echo "    already applied, skipping."
        continue
    fi

    echo "    applying ${filename}..."
    docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" < "$sql_file"
    applied_count=$((applied_count + 1))
done

echo ""
echo "✓ Done. Applied ${applied_count} new migration(s)."
