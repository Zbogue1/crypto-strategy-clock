#!/bin/bash
# Railway multi-service startup router.
#
# ROUTING IS BY EXPLICIT ENV FLAG, NOT BY SERVICE NAME.
# Name-based routing broke twice: once when a service was renamed, and once
# when two projects each had a service called "crypto-strategy-clock". An
# explicit flag makes a service's job independent of what it's called, so you
# can rename freely.
#
# Set exactly ONE of these on each Railway service:
#   FORCE_KALSHI=true  → Kalshi Golem   (kalshi_tracker.py)
#   FORCE_FOMO=true    → FOMO Golem     (fomo_tracker.py)
#   FORCE_STOCK=true   → Stock Golem    (stock_tracker.py)
#   FORCE_ORACLE=true  → legacy oracle  (crypto_oracle_v3.py --once)
#
# Legacy name matching is kept as a fallback so nothing breaks mid-migration,
# but the flag always wins.

echo "=================================================="
echo "Railway project: ${RAILWAY_PROJECT_NAME:-unknown}"
echo "Railway service: ${RAILWAY_SERVICE_NAME:-unknown}"
echo "Flags: FORCE_KALSHI=${FORCE_KALSHI:-unset} FORCE_FOMO=${FORCE_FOMO:-unset} FORCE_STOCK=${FORCE_STOCK:-unset} FORCE_ORACLE=${FORCE_ORACLE:-unset}"
echo "=================================================="

if [ "$FORCE_KALSHI" = "true" ]; then
    echo "→ Starting KALSHI GOLEM (explicit flag)"
    python kalshi_tracker.py

elif [ "$FORCE_FOMO" = "true" ]; then
    echo "→ Starting FOMO GOLEM (explicit flag)"
    python fomo_tracker.py

elif [ "$FORCE_STOCK" = "true" ]; then
    echo "→ Starting STOCK GOLEM (explicit flag)"
    python stock_tracker.py

elif [ "$FORCE_ORACLE" = "true" ]; then
    echo "→ Starting LEGACY ORACLE (explicit flag)"
    python crypto_oracle_v3.py --once

# ── Legacy fallbacks — remove once every service has a flag ────────────────
elif [ "$RAILWAY_SERVICE_NAME" = "desirable-insight" ]; then
    echo "→ Starting FOMO GOLEM (legacy name match)"
    echo "  WARNING: set FORCE_FOMO=true so renaming this service is safe."
    python fomo_tracker.py

else
    echo "!! NO ROUTING FLAG SET for service '${RAILWAY_SERVICE_NAME:-unknown}'"
    echo "!! Refusing to guess. Set one of:"
    echo "!!   FORCE_KALSHI=true / FORCE_FOMO=true / FORCE_ORACLE=true"
    echo "!! Previously this fell through to the legacy oracle, which silently"
    echo "!! ran the wrong bot and cost real debugging time."
    exit 1
fi
